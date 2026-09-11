"""SQL 扩展对象的持久化目录与账户存储（F07/F10/F11/F12 引擎侧）。

系统表物理结构与编号规则由成员二的 storage/metadata.py 定义（方案 B：动态编号、
`__` 前缀识别、bootstrap 幂等补齐），本模块只实现 Catalog 业务逻辑：
视图/触发器/索引/约束/依赖的定义读写与账户授权持久化。每张系统表首次创建时
登记进 __catalog，重开时由 PersistentCatalog 恢复后按名字解析编号。

对象读写 API 的语义与 tests/fakes/extension.py 的内存替身一致（见
docs/SQL扩展接口示例-成员三.md），契约测试对双实现并行验证。"""
from dataclasses import dataclass

from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.interfaces import RecordStorage
from minisql.contracts.models import ColumnSchema, DataType, TableSchema
from minisql.engine.auth import (
    PBKDF2_ITERATIONS, Account, AccountStore, Session, derive_key, generate_salt,
)
from minisql.storage.metadata import (
    CONSTRAINTS, DEPENDENCIES, GRANTS, INDEXES, SYSTEM_TABLES, TRIGGERS, USERS, VIEWS,
    decode_bytes, decode_name_list, encode_bytes, encode_name_list, is_system_table,
)

# 约束类别（F07）；PRIMARY KEY/FOREIGN KEY/UNIQUE 可为联合列，其余按列。
CONSTRAINT_KINDS = ("PRIMARY KEY", "FOREIGN KEY", "UNIQUE", "NOT NULL", "CHECK", "DEFAULT")


def _error(code: str, reason: str) -> MiniSQLError:
    return MiniSQLError(ErrorStage.SEMANTIC, code, reason)


@dataclass(frozen=True)
class ViewDefinition:
    """视图定义：规范化 SQL 文本 + 编译后的输出列清单。"""

    name: str
    definition: str
    columns: tuple[ColumnSchema, ...]


@dataclass(frozen=True)
class TriggerDefinition:
    """触发器定义：AFTER 行级，同一事件按创建时间（ISO 文本）先后执行。"""

    name: str
    table: str
    event: str  # INSERT / UPDATE / DELETE
    action: str
    timing: str = "AFTER"
    created_at: str = ""


@dataclass(frozen=True)
class IndexDefinition:
    """索引定义：单列或联合列；root_page 为 B+ 树根页号，登记时必填。"""

    name: str
    table: str
    columns: tuple[str, ...]
    unique: bool = False
    root_page: int | None = None


@dataclass(frozen=True)
class ConstraintDefinition:
    """约束定义（F07）：columns 为受约束列（空表示表级约束）。

    CHECK 用 expression 存条件文本，DEFAULT 用 default_text 存字面量，
    FOREIGN KEY 用 reference_table/reference_columns 存引用目标。"""

    table: str
    name: str
    kind: str
    columns: tuple[str, ...] = ()
    expression: str = ""
    reference_table: str = ""
    reference_columns: tuple[str, ...] = ()
    default_text: str = ""


def _ensure_system_table(storage: RecordStorage, catalog, schema: TableSchema) -> TableSchema:
    """创建或解析系统表：新库创建后登记进 __catalog；重开按名字解析编号。

    必须先查目录再创建：动态编号下重复 create_table 会分配新的孤儿表
    （DUPLICATE_TABLE 只对已存在编号生效），因此以目录登记为准。"""
    existing = catalog._resolve_internal(schema.name)
    if existing is not None:
        return existing
    try:
        assigned = storage.create_table(schema)
        catalog.register_table(assigned)
        return assigned
    except MiniSQLError as error:
        if error.code != "DUPLICATE_TABLE":
            raise
    resolved = catalog._resolve_internal(schema.name)
    if resolved is None:
        raise _error("INVALID_RECORD", f"系统表 {schema.name} 已存在但未在目录中登记")
    return resolved


def _next_id(storage: RecordStorage, table: TableSchema) -> int:
    return 1 + max((record.row[0] for record in storage.scan(table)), default=0)


def _delete_rows(storage: RecordStorage, table: TableSchema, predicate) -> int:
    records = [record for record in storage.scan(table) if predicate(record.row)]
    for record in records:
        storage.delete(table, record.record_id)
    return len(records)


class PersistentObjectCatalog:
    """视图/触发器/索引/约束/依赖的持久化目录（对象读写 API）。"""

    def __init__(self, storage: RecordStorage, catalog) -> None:
        self.storage = storage
        self.catalog = catalog
        self._views: dict[str, ViewDefinition] = {}
        self._view_ids: dict[str, int] = {}
        self._triggers: dict[str, TriggerDefinition] = {}
        self._indexes: dict[str, IndexDefinition] = {}
        self._constraints: dict[str, dict[str, ConstraintDefinition]] = {}
        self._deps: dict[tuple[str, str], set[tuple[str, str]]] = {}

    def bootstrap(self) -> None:
        """按成员二 SYSTEM_TABLES 顺序幂等补齐全部系统表，再恢复对象定义。"""
        for schema in SYSTEM_TABLES:
            _ensure_system_table(self.storage, self.catalog, schema)
        self._restore()

    def _table(self, schema: TableSchema) -> TableSchema:
        resolved = self.catalog._resolve_internal(schema.name)
        if resolved is None:
            raise _error("INVALID_RECORD", f"系统表 {schema.name} 未初始化")
        return resolved

    def _column_indexes(self, table: str, columns: tuple[str, ...]) -> tuple[int, ...]:
        """把列名解析为表内序号；表或列不存在时报错（约束登记用）。"""
        if is_system_table(table):
            raise _error("PROTECTED_TABLE", f"系统表 {table} 不接受用户对象定义")
        schema = self.catalog.get_table(table)
        if schema is None:
            raise _error("UNKNOWN_TABLE", table)
        names = [column.name for column in schema.columns]
        indexes = []
        for name in columns:
            if name not in names:
                raise _error("UNKNOWN_COLUMN", name)
            indexes.append(names.index(name))
        return tuple(indexes)

    # ---------- 恢复 ----------

    def _restore(self) -> None:
        views: dict[str, ViewDefinition] = {}
        view_ids: dict[str, int] = {}
        definitions: dict[int, tuple[str, str]] = {}
        columns_by_view: dict[int, dict[int, ColumnSchema]] = {}
        for record in self.storage.scan(self._table(VIEWS)):
            view_id, name, definition, column_index, column_name, column_type = record.row
            definitions.setdefault(view_id, (name, definition))
            columns_by_view.setdefault(view_id, {})[column_index] = (
                ColumnSchema(column_name, DataType(column_type))
            )
        for view_id, (name, definition) in definitions.items():
            columns = tuple(columns_by_view[view_id][index] for index in sorted(columns_by_view[view_id]))
            views[name.lower()] = ViewDefinition(name, definition, columns)
            view_ids[name.lower()] = view_id

        triggers: dict[str, TriggerDefinition] = {}
        for record in self.storage.scan(self._table(TRIGGERS)):
            _, name, table_name, event, timing, action, created_at = record.row
            triggers[name.lower()] = TriggerDefinition(
                name, table_name, event, action, timing, created_at)

        indexes: dict[str, IndexDefinition] = {}
        columns_by_index: dict[int, dict[int, str]] = {}
        meta_by_index: dict[int, tuple[str, str, bool, int]] = {}
        for record in self.storage.scan(self._table(INDEXES)):
            index_id, name, table_name, unique_flag, column_index, column_name, root_page = record.row
            meta_by_index.setdefault(index_id, (name, table_name, bool(unique_flag), root_page))
            columns_by_index.setdefault(index_id, {})[column_index] = column_name
        for index_id, (name, table_name, unique, root_page) in meta_by_index.items():
            columns = tuple(columns_by_index[index_id][index] for index in sorted(columns_by_index[index_id]))
            indexes[name.lower()] = IndexDefinition(name, table_name, columns, unique, root_page)

        constraints: dict[str, dict[str, ConstraintDefinition]] = {}
        pending: dict[tuple[str, str], dict] = {}
        for record in self.storage.scan(self._table(CONSTRAINTS)):
            table_name, column_index, name, kind, expression, reference_table, reference_columns, default_text = record.row
            key = (table_name.lower(), name.lower())
            entry = pending.setdefault(key, {
                "table": table_name, "name": name, "kind": kind, "indexes": [],
                "expression": expression or "", "reference_table": reference_table or "",
                "reference_columns": decode_name_list(reference_columns),
                "default_text": default_text or "",
            })
            if column_index >= 0:
                entry["indexes"].append(column_index)
        for (table_key, _), entry in pending.items():
            schema = self.catalog.get_table(table_key)
            if schema is None:
                continue  # 表已删除的孤儿约束记录不恢复
            names = [column.name for column in schema.columns]
            columns = tuple(names[index] for index in sorted(entry["indexes"]) if index < len(names))
            constraint = ConstraintDefinition(
                entry["table"], entry["name"], entry["kind"], columns,
                entry["expression"], entry["reference_table"],
                entry["reference_columns"], entry["default_text"])
            constraints.setdefault(table_key, {})[entry["name"].lower()] = constraint

        deps: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for record in self.storage.scan(self._table(DEPENDENCIES)):
            object_type, object_name, depends_on_type, depends_on_name = record.row
            deps.setdefault((object_type, object_name.lower()), set()).add(
                (depends_on_type, depends_on_name.lower()))
        self._views, self._view_ids = views, view_ids
        self._triggers, self._indexes, self._deps = triggers, indexes, deps
        self._constraints = constraints

    # ---------- 视图 ----------

    def register_view(self, view: ViewDefinition) -> None:
        key = view.name.lower()
        if key in self._views:
            raise _error("DUPLICATE_OBJECT", f"视图 {view.name} 已存在")
        if not view.columns:
            raise _error("INVALID_RECORD", f"视图 {view.name} 缺少输出列")
        table = self._table(VIEWS)
        view_id = _next_id(self.storage, table)
        for index, column in enumerate(view.columns):
            self.storage.insert(table, (view_id, key, view.definition, index, column.name, column.data_type.value))
        self.storage.flush()
        self._views[key] = ViewDefinition(key, view.definition, view.columns)
        self._view_ids[key] = view_id

    def get_view(self, name: str) -> ViewDefinition | None:
        return self._views.get(name.lower())

    def list_views(self) -> tuple[ViewDefinition, ...]:
        return tuple(self._views.values())

    def unregister_view(self, name: str) -> None:
        key = name.lower()
        if key not in self._views:
            raise _error("UNKNOWN_OBJECT", f"视图 {name} 不存在")
        table = self._table(VIEWS)
        view_id = self._view_ids[key]
        _delete_rows(self.storage, table, lambda row: row[0] == view_id)
        self.storage.flush()
        del self._views[key]
        del self._view_ids[key]

    # ---------- 触发器 ----------

    def register_trigger(self, trigger: TriggerDefinition) -> None:
        key = trigger.name.lower()
        if key in self._triggers:
            raise _error("DUPLICATE_OBJECT", f"触发器 {trigger.name} 已存在")
        table = self._table(TRIGGERS)
        trigger_id = _next_id(self.storage, table)
        self.storage.insert(table, (
            trigger_id, key, trigger.table.lower(), trigger.event.upper(),
            trigger.timing.upper(), trigger.action, trigger.created_at,
        ))
        self.storage.flush()
        self._triggers[key] = TriggerDefinition(
            key, trigger.table, trigger.event, trigger.action, trigger.timing, trigger.created_at)

    def get_trigger(self, name: str) -> TriggerDefinition | None:
        return self._triggers.get(name.lower())

    def get_triggers(self, table: str, event: str) -> tuple[TriggerDefinition, ...]:
        """指定表的指定事件的全部触发器，按创建时间先后排序（同刻按名称）。"""
        key = table.lower()
        matched = [t for t in self._triggers.values() if t.table == key and t.event == event.upper()]
        return tuple(sorted(matched, key=lambda trigger: (trigger.created_at, trigger.name)))

    def unregister_trigger(self, name: str) -> None:
        key = name.lower()
        if key not in self._triggers:
            raise _error("UNKNOWN_OBJECT", f"触发器 {name} 不存在")
        table = self._table(TRIGGERS)
        _delete_rows(self.storage, table, lambda row: row[1] == key)
        self.storage.flush()
        del self._triggers[key]

    # ---------- 索引 ----------

    def register_index(self, index: IndexDefinition) -> None:
        key = index.name.lower()
        if key in self._indexes:
            raise _error("DUPLICATE_OBJECT", f"索引 {index.name} 已存在")
        if index.root_page is None:
            raise _error("INVALID_RECORD", f"索引 {index.name} 缺少 root_page，无法重开恢复")
        table = self._table(INDEXES)
        index_id = _next_id(self.storage, table)
        for column_index, column in enumerate(index.columns):
            self.storage.insert(table, (
                index_id, key, index.table.lower(), int(index.unique), column_index, column, index.root_page,
            ))
        self.storage.flush()
        self._indexes[key] = IndexDefinition(key, index.table, index.columns, index.unique, index.root_page)

    def get_index(self, name: str) -> IndexDefinition | None:
        return self._indexes.get(name.lower())

    def get_indexes(self, table: str) -> tuple[IndexDefinition, ...]:
        key = table.lower()
        return tuple(index for index in self._indexes.values() if index.table == key)

    def unregister_index(self, name: str) -> None:
        key = name.lower()
        if key not in self._indexes:
            raise _error("UNKNOWN_OBJECT", f"索引 {name} 不存在")
        table = self._table(INDEXES)
        _delete_rows(self.storage, table, lambda row: row[1] == key)
        self.storage.flush()
        del self._indexes[key]

    # ---------- 约束（F07） ----------

    def register_constraint(self, constraint: ConstraintDefinition) -> None:
        if constraint.kind not in CONSTRAINT_KINDS:
            raise _error("UNKNOWN_CONSTRAINT_KIND", constraint.kind)
        table_key = constraint.table.lower()
        name_key = constraint.name.lower()
        if name_key in self._constraints.get(table_key, {}):
            raise _error("DUPLICATE_OBJECT", f"约束 {constraint.name} 已存在")
        indexes = self._column_indexes(constraint.table, constraint.columns)
        rows_indexes = indexes if indexes else (-1,)
        table = self._table(CONSTRAINTS)
        reference_columns = encode_name_list(constraint.reference_columns)
        for column_index in rows_indexes:
            self.storage.insert(table, (
                table_key, column_index, name_key, constraint.kind, constraint.expression,
                constraint.reference_table.lower(), reference_columns, constraint.default_text,
            ))
        self.storage.flush()
        self._constraints.setdefault(table_key, {})[name_key] = ConstraintDefinition(
            constraint.table, constraint.name, constraint.kind, constraint.columns,
            constraint.expression, constraint.reference_table, constraint.reference_columns,
            constraint.default_text)

    def get_constraints(self, table: str) -> tuple[ConstraintDefinition, ...]:
        """指定表的全部约束，按约束名排序。"""
        key = table.lower()
        return tuple(sorted(self._constraints.get(key, {}).values(), key=lambda item: item.name))

    def unregister_constraints(self, table: str, name: str | None = None) -> None:
        """移除指定表的全部约束，或仅移除具名约束。"""
        table_key = table.lower()
        names = [name.lower()] if name is not None else list(self._constraints.get(table_key, {}))
        if not names:
            if name is not None:
                raise _error("UNKNOWN_OBJECT", f"约束 {name} 不存在")
            return
        for name_key in names:
            if name_key not in self._constraints.get(table_key, {}):
                raise _error("UNKNOWN_OBJECT", f"约束 {name} 不存在")
        storage_table = self._table(CONSTRAINTS)
        _delete_rows(self.storage, storage_table, lambda row: (
            row[0] == table_key and row[2] in names))
        self.storage.flush()
        for name_key in names:
            del self._constraints[table_key][name_key]

    # ---------- 依赖 ----------

    def add_dependency(self, object_type: str, object_name: str,
                       depends_on_type: str, depends_on_name: str) -> None:
        key = (object_type, object_name.lower())
        edge = (depends_on_type, depends_on_name.lower())
        if key in self._deps and edge in self._deps[key]:
            return
        table = self._table(DEPENDENCIES)
        self.storage.insert(table, (object_type, object_name.lower(), *edge))
        self.storage.flush()
        self._deps.setdefault(key, set()).add(edge)

    def remove_object(self, object_type: str, object_name: str) -> None:
        """对象删除/重建时移除它自身的全部依赖记录。"""
        key = (object_type, object_name.lower())
        if key in self._deps:
            table = self._table(DEPENDENCIES)
            _delete_rows(self.storage, table,
                         lambda row: (row[0], row[1]) == key)
            self.storage.flush()
            del self._deps[key]

    def dependencies(self, object_type: str, object_name: str) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._deps.get((object_type, object_name.lower()), ())))

    def dependents(self, object_type: str, object_name: str) -> tuple[tuple[str, str], ...]:
        target = (object_type, object_name.lower())
        return tuple(sorted(key for key, deps in self._deps.items() if target in deps))

    def assert_droppable(self, object_type: str, object_name: str) -> None:
        dependents = self.dependents(object_type, object_name)
        if dependents:
            raise _error(
                "DEPENDENT_OBJECT",
                f"{object_type} {object_name} 被依赖对象使用：{dependents}")


class ExtendedCatalogAdapter:
    """编译器只读目录适配器：把持久化对象翻译成编译器契约（extensions 类型）。

    Binder/优化器经此读取视图、索引、触发器、账户与依赖；未实现可选接口
    （如无索引枚举）时编译器按契约保守回退到全表扫描。"""

    def __init__(self, catalog, objects, accounts) -> None:
        self.catalog = catalog
        self.objects = objects
        self.accounts = accounts

    # ---- 表（转发持久化目录） ----
    def get_table(self, name: str):
        return self.catalog.get_table(name)

    def list_tables(self):
        return self.catalog.list_tables()

    # ---- 视图 ----
    def get_view(self, name: str):
        from minisql.contracts.extensions import ViewDefinition as CompilerView
        view = self.objects.get_view(name)
        if view is None:
            return None
        return CompilerView(
            name=view.name, query=view.definition,
            columns=tuple(column.name for column in view.columns))

    # ---- 索引 ----
    def get_index(self, name: str):
        from minisql.contracts.extensions import IndexDefinition as CompilerIndex
        index = self.objects.get_index(name)
        if index is None:
            return None
        return CompilerIndex(name=index.name, table=index.table,
                             columns=index.columns, unique=index.unique, available=True)

    def list_indexes(self, table: str):
        return tuple(self.get_index(index.name) for index in self.objects.get_indexes(table))

    # ---- 触发器 ----
    def get_trigger(self, name: str):
        from minisql.contracts.extensions import TriggerDefinition as CompilerTrigger
        trigger = self.objects.get_trigger(name)
        if trigger is None:
            return None
        return CompilerTrigger(name=trigger.name, table=trigger.table,
                               event=trigger.event, writes=())

    def list_triggers(self):
        return tuple(self.get_trigger(trigger.name) for trigger in self.objects._triggers.values())

    # ---- 账户与库名 ----
    def has_database(self, name: str) -> bool:
        return name.lower() == "main"

    def get_account(self, name: str):
        return self.accounts.accounts.get(name.lower())

    # ---- 依赖 ----
    def get_dependencies(self, kind: str, name: str):
        from minisql.contracts.extensions import ObjectDependency
        return tuple(ObjectDependency(kind=dependency_kind, name=dependency_name)
                     for dependency_kind, dependency_name
                     in self.objects.dependencies(kind, name))


class PersistentAccountStore(AccountStore):
    """__users/__grants 持久化的账户与授权存储；接口面与 AccountStore 一致。"""

    def __init__(self, storage: RecordStorage, catalog) -> None:
        super().__init__()
        self.storage = storage
        self.catalog = catalog

    def bootstrap(self) -> None:
        for schema in (USERS, GRANTS):
            _ensure_system_table(self.storage, self.catalog, schema)
        users: dict[str, Account] = {}
        for record in self.storage.scan(self._table(USERS)):
            _, account_id, name, salt, key, iterations, is_admin = record.row
            users[name.lower()] = Account(
                name, decode_bytes(salt), decode_bytes(key), bool(is_admin), iterations, account_id,
            )
        grants: dict[tuple[str, str, str], set[str]] = {}
        for record in self.storage.scan(self._table(GRANTS)):
            user, object_type, object_name, permission = record.row
            grants.setdefault((user.lower(), object_type, object_name.lower()), set()).add(permission)
        self.accounts, self.grants = users, grants

    def _table(self, schema: TableSchema) -> TableSchema:
        resolved = self.catalog._resolve_internal(schema.name)
        if resolved is None:
            raise MiniSQLError(ErrorStage.SEMANTIC, "INVALID_RECORD", f"系统表 {schema.name} 未初始化")
        return resolved

    def create_account(self, name: str, password: str, is_admin: bool = False,
                       iterations: int = PBKDF2_ITERATIONS) -> Account:
        salt = generate_salt()
        account = Account(name.lower(), salt, derive_key(password, salt, iterations),
                          is_admin, iterations)
        self.add(account)
        return account

    def add(self, account: Account) -> None:
        super().add(account)  # 重复账户检查
        table = self._table(USERS)
        user_id = _next_id(self.storage, table)
        self.storage.insert(table, (
            user_id, account.account_id, account.name, encode_bytes(account.salt),
            encode_bytes(account.key), account.iterations, int(account.is_admin),
        ))
        self.storage.flush()

    def grant(self, user: str, permission: str, object_kind: str, object_name: str) -> None:
        super().grant(user, permission, object_kind, object_name)  # 校验
        table = self._table(GRANTS)
        self.storage.insert(table, (user.lower(), object_kind, object_name.lower(), permission))
        self.storage.flush()

    def revoke(self, user: str, permission: str, object_kind: str, object_name: str) -> None:
        super().revoke(user, permission, object_kind, object_name)
        table = self._table(GRANTS)
        _delete_rows(self.storage, table, lambda row: (
            row[0] == user.lower() and row[1] == object_kind
            and row[2] == object_name.lower() and row[3] == permission))
        self.storage.flush()

    def remove_account(self, admin: Session | None, name: str) -> None:
        super().remove_account(admin, name)  # 管理员/最后管理员/存在性检查 + 内存清理
        key = name.lower()
        users_table = self._table(USERS)
        _delete_rows(self.storage, users_table, lambda row: row[2] == key)
        grants_table = self._table(GRANTS)
        _delete_rows(self.storage, grants_table, lambda row: row[0] == key)
        self.storage.flush()
