"""SQL 扩展对象的持久化目录与账户存储（F06-F12 引擎侧基础）。

六张对象系统表（__views/__triggers/__indexes/__users/__grants/__dependencies）
采用动态编号：新建库按固定顺序创建后占据 1..6，用户表从 7 开始；旧库无冲突
（系统表排在既有用户表编号之后）。每张系统表首次创建时登记进 __catalog
（表名以 __ 开头，公开 get_table/list_tables 不暴露），重开时由
PersistentCatalog 恢复后按名字解析编号。

对象读写 API 的语义与 tests/fakes/extension.py 的内存替身一致（见
docs/SQL扩展接口示例-成员三.md），契约测试对双实现并行验证。"""
from dataclasses import dataclass

from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.interfaces import RecordStorage
from minisql.contracts.models import ColumnSchema, DataType, TableSchema
from minisql.engine.auth import (
    PBKDF2_ITERATIONS, Account, AccountStore, Session, derive_key, generate_salt,
)


def _error(code: str, reason: str) -> MiniSQLError:
    return MiniSQLError(ErrorStage.SEMANTIC, code, reason)


VIEWS_CATALOG = TableSchema("__views", (
    ColumnSchema("view_id", DataType.INT),
    ColumnSchema("view_name", DataType.VARCHAR),
    ColumnSchema("definition", DataType.VARCHAR),
    ColumnSchema("column_index", DataType.INT),
    ColumnSchema("column_name", DataType.VARCHAR),
    ColumnSchema("column_type", DataType.VARCHAR),
))
TRIGGERS_CATALOG = TableSchema("__triggers", (
    ColumnSchema("trigger_id", DataType.INT),
    ColumnSchema("trigger_name", DataType.VARCHAR),
    ColumnSchema("table_name", DataType.VARCHAR),
    ColumnSchema("event", DataType.VARCHAR),
    ColumnSchema("action", DataType.VARCHAR),
    ColumnSchema("created_order", DataType.INT),
))
INDEXES_CATALOG = TableSchema("__indexes", (
    ColumnSchema("index_id", DataType.INT),
    ColumnSchema("index_name", DataType.VARCHAR),
    ColumnSchema("table_name", DataType.VARCHAR),
    ColumnSchema("unique_flag", DataType.INT),
    ColumnSchema("column_index", DataType.INT),
    ColumnSchema("column_name", DataType.VARCHAR),
))
USERS_CATALOG = TableSchema("__users", (
    ColumnSchema("user_id", DataType.INT),
    ColumnSchema("account_id", DataType.VARCHAR),
    ColumnSchema("user_name", DataType.VARCHAR),
    ColumnSchema("salt", DataType.VARCHAR),
    ColumnSchema("key", DataType.VARCHAR),
    ColumnSchema("iterations", DataType.INT),
    ColumnSchema("is_admin", DataType.INT),
))
GRANTS_CATALOG = TableSchema("__grants", (
    ColumnSchema("user_name", DataType.VARCHAR),
    ColumnSchema("object_type", DataType.VARCHAR),
    ColumnSchema("object_name", DataType.VARCHAR),
    ColumnSchema("permission", DataType.VARCHAR),
))
DEPENDENCIES_CATALOG = TableSchema("__dependencies", (
    ColumnSchema("object_type", DataType.VARCHAR),
    ColumnSchema("object_name", DataType.VARCHAR),
    ColumnSchema("depends_on_type", DataType.VARCHAR),
    ColumnSchema("depends_on_name", DataType.VARCHAR),
))


@dataclass(frozen=True)
class ViewDefinition:
    """视图定义：规范化 SQL 文本 + 编译后的输出列清单。"""

    name: str
    definition: str
    columns: tuple[ColumnSchema, ...]


@dataclass(frozen=True)
class TriggerDefinition:
    """触发器定义：AFTER 行级，同一事件多个触发器按 created_order 升序执行。"""

    name: str
    table: str
    event: str  # INSERT / UPDATE / DELETE
    action: str
    created_order: int = 0


@dataclass(frozen=True)
class IndexDefinition:
    """索引定义：单列或联合列，唯一索引另设 unique 标志。"""

    name: str
    table: str
    columns: tuple[str, ...]
    unique: bool = False


def _ensure_system_table(storage: RecordStorage, catalog, schema: TableSchema) -> TableSchema:
    """创建或解析对象系统表：新库创建后登记进 __catalog；重开按名字解析编号。

    必须先查目录再创建：动态编号下重复 create_table 会分配新的孤儿表
    （DUPLICATE_TABLE 只对已存在编号生效），因此以目录登记为准。
    """
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
    """视图/触发器/索引定义与依赖的持久化目录（对象读写 API）。"""

    def __init__(self, storage: RecordStorage, catalog) -> None:
        self.storage = storage
        self.catalog = catalog
        self._views: dict[str, ViewDefinition] = {}
        self._view_ids: dict[str, int] = {}
        self._triggers: dict[str, TriggerDefinition] = {}
        self._indexes: dict[str, IndexDefinition] = {}
        self._deps: dict[tuple[str, str], set[tuple[str, str]]] = {}

    def bootstrap(self) -> None:
        for schema in (VIEWS_CATALOG, TRIGGERS_CATALOG, INDEXES_CATALOG, DEPENDENCIES_CATALOG):
            _ensure_system_table(self.storage, self.catalog, schema)
        self._restore()

    def _table(self, schema: TableSchema) -> TableSchema:
        resolved = self.catalog._resolve_internal(schema.name)
        if resolved is None:
            raise _error("INVALID_RECORD", f"系统表 {schema.name} 未初始化")
        return resolved

    # ---------- 恢复 ----------

    def _restore(self) -> None:
        views: dict[str, ViewDefinition] = {}
        view_ids: dict[str, int] = {}
        definitions: dict[int, tuple[str, str]] = {}
        columns_by_view: dict[int, dict[int, ColumnSchema]] = {}
        for record in self.storage.scan(self._table(VIEWS_CATALOG)):
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
        for record in self.storage.scan(self._table(TRIGGERS_CATALOG)):
            _, name, table_name, event, action, created_order = record.row
            triggers[name.lower()] = TriggerDefinition(name, table_name, event, action, created_order)

        indexes: dict[str, IndexDefinition] = {}
        columns_by_index: dict[int, dict[int, str]] = {}
        meta_by_index: dict[int, tuple[str, str, bool]] = {}
        for record in self.storage.scan(self._table(INDEXES_CATALOG)):
            index_id, name, table_name, unique_flag, column_index, column_name = record.row
            meta_by_index.setdefault(index_id, (name, table_name, bool(unique_flag)))
            columns_by_index.setdefault(index_id, {})[column_index] = column_name
        for index_id, (name, table_name, unique) in meta_by_index.items():
            columns = tuple(columns_by_index[index_id][index] for index in sorted(columns_by_index[index_id]))
            indexes[name.lower()] = IndexDefinition(name, table_name, columns, unique)

        deps: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for record in self.storage.scan(self._table(DEPENDENCIES_CATALOG)):
            object_type, object_name, depends_on_type, depends_on_name = record.row
            deps.setdefault((object_type, object_name.lower()), set()).add(
                (depends_on_type, depends_on_name.lower()))
        self._views, self._view_ids = views, view_ids
        self._triggers, self._indexes, self._deps = triggers, indexes, deps

    # ---------- 视图 ----------

    def register_view(self, view: ViewDefinition) -> None:
        key = view.name.lower()
        if key in self._views:
            raise _error("DUPLICATE_OBJECT", f"视图 {view.name} 已存在")
        if not view.columns:
            raise _error("INVALID_RECORD", f"视图 {view.name} 缺少输出列")
        table = self._table(VIEWS_CATALOG)
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
        table = self._table(VIEWS_CATALOG)
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
        table = self._table(TRIGGERS_CATALOG)
        trigger_id = _next_id(self.storage, table)
        self.storage.insert(table, (
            trigger_id, key, trigger.table.lower(), trigger.event.upper(),
            trigger.action, trigger.created_order,
        ))
        self.storage.flush()
        self._triggers[key] = TriggerDefinition(
            key, trigger.table, trigger.event, trigger.action, trigger.created_order)

    def get_trigger(self, name: str) -> TriggerDefinition | None:
        return self._triggers.get(name.lower())

    def get_triggers(self, table: str, event: str) -> tuple[TriggerDefinition, ...]:
        """指定表的指定事件的全部触发器，按创建先后排序。"""
        key = table.lower()
        matched = [t for t in self._triggers.values() if t.table == key and t.event == event.upper()]
        return tuple(sorted(matched, key=lambda trigger: trigger.created_order))

    def unregister_trigger(self, name: str) -> None:
        key = name.lower()
        if key not in self._triggers:
            raise _error("UNKNOWN_OBJECT", f"触发器 {name} 不存在")
        table = self._table(TRIGGERS_CATALOG)
        _delete_rows(self.storage, table, lambda row: row[1] == key)
        self.storage.flush()
        del self._triggers[key]

    # ---------- 索引 ----------

    def register_index(self, index: IndexDefinition) -> None:
        key = index.name.lower()
        if key in self._indexes:
            raise _error("DUPLICATE_OBJECT", f"索引 {index.name} 已存在")
        table = self._table(INDEXES_CATALOG)
        index_id = _next_id(self.storage, table)
        for column_index, column in enumerate(index.columns):
            self.storage.insert(table, (index_id, key, index.table.lower(), int(index.unique), column_index, column))
        self.storage.flush()
        self._indexes[key] = IndexDefinition(key, index.table, index.columns, index.unique)

    def get_index(self, name: str) -> IndexDefinition | None:
        return self._indexes.get(name.lower())

    def get_indexes(self, table: str) -> tuple[IndexDefinition, ...]:
        key = table.lower()
        return tuple(index for index in self._indexes.values() if index.table == key)

    def unregister_index(self, name: str) -> None:
        key = name.lower()
        if key not in self._indexes:
            raise _error("UNKNOWN_OBJECT", f"索引 {name} 不存在")
        table = self._table(INDEXES_CATALOG)
        _delete_rows(self.storage, table, lambda row: row[1] == key)
        self.storage.flush()
        del self._indexes[key]

    # ---------- 依赖 ----------

    def add_dependency(self, object_type: str, object_name: str,
                       depends_on_type: str, depends_on_name: str) -> None:
        key = (object_type, object_name.lower())
        edge = (depends_on_type, depends_on_name.lower())
        if key in self._deps and edge in self._deps[key]:
            return
        table = self._table(DEPENDENCIES_CATALOG)
        self.storage.insert(table, (object_type, object_name.lower(), *edge))
        self.storage.flush()
        self._deps.setdefault(key, set()).add(edge)

    def remove_object(self, object_type: str, object_name: str) -> None:
        """对象删除/重建时移除它自身的全部依赖记录。"""
        key = (object_type, object_name.lower())
        if key in self._deps:
            table = self._table(DEPENDENCIES_CATALOG)
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


class PersistentAccountStore(AccountStore):
    """__users/__grants 持久化的账户与授权存储；接口面与 AccountStore 一致。"""

    def __init__(self, storage: RecordStorage, catalog) -> None:
        super().__init__()
        self.storage = storage
        self.catalog = catalog

    def bootstrap(self) -> None:
        for schema in (USERS_CATALOG, GRANTS_CATALOG):
            _ensure_system_table(self.storage, self.catalog, schema)
        users: dict[str, Account] = {}
        for record in self.storage.scan(self._table(USERS_CATALOG)):
            _, account_id, name, salt, key, iterations, is_admin = record.row
            users[name.lower()] = Account(
                name, bytes.fromhex(salt), bytes.fromhex(key),
                bool(is_admin), iterations, account_id,
            )
        grants: dict[tuple[str, str, str], set[str]] = {}
        for record in self.storage.scan(self._table(GRANTS_CATALOG)):
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
        table = self._table(USERS_CATALOG)
        user_id = _next_id(self.storage, table)
        self.storage.insert(table, (
            user_id, account.account_id, account.name, account.salt.hex(),
            account.key.hex(), account.iterations, int(account.is_admin),
        ))
        self.storage.flush()

    def grant(self, user: str, permission: str, object_kind: str, object_name: str) -> None:
        super().grant(user, permission, object_kind, object_name)  # 校验
        table = self._table(GRANTS_CATALOG)
        self.storage.insert(table, (user.lower(), object_kind, object_name.lower(), permission))
        self.storage.flush()

    def revoke(self, user: str, permission: str, object_kind: str, object_name: str) -> None:
        super().revoke(user, permission, object_kind, object_name)
        table = self._table(GRANTS_CATALOG)
        _delete_rows(self.storage, table, lambda row: (
            row[0] == user.lower() and row[1] == object_kind
            and row[2] == object_name.lower() and row[3] == permission))
        self.storage.flush()

    def remove_account(self, admin: Session | None, name: str) -> None:
        super().remove_account(admin, name)  # 管理员/最后管理员/存在性检查 + 内存清理
        key = name.lower()
        users_table = self._table(USERS_CATALOG)
        _delete_rows(self.storage, users_table, lambda row: row[2] == key)
        grants_table = self._table(GRANTS_CATALOG)
        _delete_rows(self.storage, grants_table, lambda row: row[0] == key)
        self.storage.flush()
