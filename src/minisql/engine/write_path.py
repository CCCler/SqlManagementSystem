"""扩展 DML/DDL 写路径：约束检查、索引同步维护、表结构变更与对象定义执行。

由 PlanExecutor 混入使用；依赖由 Database 注入的 objects（对象目录）与
accounts（账户存储）。约束错误码：NOT_NULL_VIOLATION、DUPLICATE_KEY、
CHECK_VIOLATION、FOREIGN_KEY_VIOLATION（运行时新码，需组内周知）。"""
from datetime import date, datetime, time
from dataclasses import replace
from decimal import Decimal

from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.models import DataType, ExecutionResult, TableSchema
from minisql.engine.expr import (
    Row as RuntimeRow, RowContext, deserialize_expr, evaluate, serialize_expr,
)


def _write_error(code: str, reason: str) -> MiniSQLError:
    return MiniSQLError(ErrorStage.EXECUTION, code, reason)


_OPERATOR_PERMISSIONS = {
    "Insert": "INSERT",
    "Update": "UPDATE",
    "Delete": "DELETE",
    "CreateTable": "CREATE TABLE",
    "AlterTable": "ALTER",
    "CreateIndex": "CREATE INDEX",
    "CreateView": "CREATE VIEW",
    "CreateTrigger": "CREATE TRIGGER",
    "DropView": "DROP",
    "DropIndex": "DROP",
    "DropTrigger": "DROP",
}


class _ReloadCatalogView:
    """触发器定义重载视图：隐藏正在重载的触发器，避免重复检查误报。"""

    def __init__(self, view, hidden: str):
        self._view = view
        self._hidden = hidden

    def __getattr__(self, name):
        return getattr(self._view, name)

    def get_trigger(self, name):
        if name.lower() == self._hidden:
            return None
        return self._view.get_trigger(name)


class WritePathMixin:
    """扩展写路径；宿主类需提供 storage、catalog、executor 侧注入的 objects/accounts。"""

    # ---------- 通用工具 ----------

    def _require_objects(self):
        if getattr(self, "objects", None) is None:
            raise _write_error("FEATURE_NOT_EXECUTABLE", "对象目录未接入，无法执行扩展 DDL")

    # ---------- 统一鉴权（初始化模式：无账户时不强制） ----------

    def _enforcing(self) -> bool:
        return self.accounts is not None and bool(self.accounts.accounts)

    def _require_permission(self, permission, object_kind=None, object_name=None):
        if not self._enforcing():
            return
        self.accounts.require(getattr(self, "session", None), permission,
                              object_kind, object_name)

    def _enforce_plan_permissions(self, plan):
        """统一入口检查：按计划收集所需的表级权限，覆盖查询/写入/DDL 与视图展开。"""
        if not self._enforcing():
            return
        from minisql.contracts.extensions import ExtendedPlan
        from minisql.contracts.plans import (
            CreateTable as LegacyCreate, Delete as LegacyDelete, DropTable as LegacyDrop,
            Explain as LegacyExplain, Filter, Insert as LegacyInsert, Project,
            EmptyScan, QueryPlan, SeqScan, Update as LegacyUpdate,
        )

        permissions: dict[tuple[str, str], str] = {}

        def need(permission, table):
            permissions[(permission, table.lower())] = permission

        def visit(node):
            if isinstance(node, ExtendedPlan):
                operator = node.operator
                attributes = dict(node.attributes)
                if operator in ("TableScan", "IndexScan"):
                    need("SELECT", attributes.get("table", ""))
                elif operator in _OPERATOR_PERMISSIONS:
                    need(_OPERATOR_PERMISSIONS[operator], attributes.get("table", ""))
                elif operator == "DropTable":
                    need("DROP", attributes.get("table", ""))
                for child in node.children:
                    if isinstance(child, ExtendedPlan):
                        visit(child)
                for expression in node.expressions:
                    for arg in expression.args:
                        if isinstance(arg, ExtendedPlan):
                            visit(arg)
                return
            if isinstance(node, SeqScan):
                need("SELECT", node.schema.name)
            elif isinstance(node, Filter):
                visit(node.source)
            elif isinstance(node, Project):
                visit(node.source)
            elif isinstance(node, EmptyScan):
                return
            elif isinstance(node, LegacyInsert):
                need("INSERT", node.schema.name)
            elif isinstance(node, LegacyUpdate):
                need("UPDATE", node.schema.name)
                visit(node.source)
            elif isinstance(node, LegacyDelete):
                need("DELETE", node.schema.name)
                visit(node.source)
            elif isinstance(node, LegacyCreate):
                need("CREATE TABLE", node.schema.name)
            elif isinstance(node, LegacyDrop):
                need("DROP", node.schema.name)
            elif isinstance(node, LegacyExplain):
                visit(node.plan)

        visit(plan)
        for (permission, table) in permissions:
            self.accounts.require(getattr(self, "session", None), permission, "table", table)

    def enforce_write_permission(self, permission, table):
        """行级写入与 DDL 处理器使用；表名统一小写。"""
        self._require_permission(permission, "table", table)

    def _table_schema(self, name: str) -> TableSchema:
        schema = self.catalog.get_table(name)
        if schema is None:
            raise _write_error("UNKNOWN_TABLE", name)
        return schema

    def _row_dict(self, plan, row):
        """把一行绑定到计划引用的全部 (scope, source) 键（单来源语句通用）。"""
        from minisql.engine.executor import _collect_bindings
        return {key: tuple(row) for key in _collect_bindings(plan)}

    def _default_value(self, schema, column):
        """列默认值：取 __constraints 中该列的 DEFAULT 常量表达式求值。"""
        if self.objects is None:
            return None
        for constraint in self.objects.get_constraints(schema.name):
            if constraint.kind == "DEFAULT" and column.name in constraint.columns:
                expression = deserialize_expr(
                    constraint.default_text, tuple(c.name for c in schema.columns))
                return evaluate(expression, RowContext(RuntimeRow()))
        return None

    # ---------- 约束检查 ----------

    def _check_constraints(self, schema, row, skip_row=None):
        """写入前检查：NOT NULL、主键/唯一、CHECK（仅 FALSE 违规）、外键 MATCH SIMPLE。"""
        columns = [column.name for column in schema.columns]
        for position, column in enumerate(schema.columns):
            if not column.nullable and row[position] is None:
                raise _write_error("NOT_NULL_VIOLATION", f"列 {column.name} 不允许 NULL")
        if self.objects is None:
            return
        for constraint in self.objects.get_constraints(schema.name):
            kind = constraint.kind
            if kind in ("PRIMARY KEY", "UNIQUE"):
                indexes = [columns.index(name) for name in constraint.columns]
                key = tuple(row[index] for index in indexes)
                if any(value is None for value in key):
                    if kind == "PRIMARY KEY":
                        raise _write_error("NOT_NULL_VIOLATION",
                                           f"主键 {constraint.name} 不允许 NULL")
                    continue  # UNIQUE 允许含 NULL 的重复键
                for record in self.storage.scan(schema):
                    if skip_row is not None and tuple(record.row) == tuple(skip_row):
                        continue
                    if tuple(record.row[index] for index in indexes) == key:
                        raise _write_error("DUPLICATE_KEY",
                                           f"违反唯一性约束：{constraint.name}")
            elif kind == "CHECK":
                expression = deserialize_expr(constraint.expression, tuple(columns))
                value = evaluate(expression, RowContext(RuntimeRow({(0, 0): tuple(row)})))
                if value is False:
                    raise _write_error("CHECK_VIOLATION", f"违反检查约束：{constraint.name}")
            elif kind == "FOREIGN KEY":
                indexes = [columns.index(name) for name in constraint.columns]
                key = tuple(row[index] for index in indexes)
                if any(value is None for value in key):
                    continue  # MATCH SIMPLE：含 NULL 的组合不检查
                parent = self.catalog.get_table(constraint.reference_table)
                if parent is None:
                    raise _write_error("FOREIGN_KEY_VIOLATION",
                                       f"引用表不存在：{constraint.reference_table}")
                parent_columns = [column.name for column in parent.columns]
                ref_indexes = [parent_columns.index(name) for name in constraint.reference_columns]
                if not any(tuple(record.row[index] for index in ref_indexes) == key
                           for record in self.storage.scan(parent)):
                    raise _write_error("FOREIGN_KEY_VIOLATION",
                                       f"违反外键约束：{constraint.name}")

    def _check_parent_references(self, schema, row):
        """删除/更新父行前检查子表外键引用（首版拒绝破坏引用，不隐式级联）。"""
        if self.objects is None:
            return
        for constraint in self.objects.all_constraints():
            if constraint.kind != "FOREIGN KEY" or constraint.reference_table != schema.name:
                continue
            child = self.catalog.get_table(constraint.table)
            if child is None:
                continue
            child_columns = [column.name for column in child.columns]
            parent_columns = [column.name for column in schema.columns]
            child_indexes = [child_columns.index(name) for name in constraint.columns]
            parent_indexes = [parent_columns.index(name) for name in constraint.reference_columns]
            key = tuple(row[index] for index in parent_indexes)
            if any(value is None for value in key):
                continue
            for record in self.storage.scan(child):
                if tuple(record.row[index] for index in child_indexes) == key:
                    raise _write_error("FOREIGN_KEY_VIOLATION",
                                       f"被引用行仍被 {constraint.name} 使用")

    # ---------- 索引维护 ----------

    def _index_defs(self, schema):
        if self.objects is None:
            return ()
        return self.objects.get_indexes(schema.name)

    def _index_tree(self, schema, index_def):
        from minisql.storage.index import BTreeIndex
        name_to_column = {column.name: column for column in schema.columns}
        columns = tuple(name_to_column[name] for name in index_def.columns)
        return BTreeIndex(self.storage.pages, self.storage.buffer, columns,
                          index_def.root_page, schema.table_id)

    def _index_key(self, schema, index_def, row):
        name_to_index = {column.name: i for i, column in enumerate(schema.columns)}
        return tuple(row[name_to_index[name]] for name in index_def.columns)

    def _index_insert(self, schema, row, record_id):
        for index_def in self._index_defs(schema):
            index = self._index_tree(schema, index_def)
            key = self._index_key(schema, index_def, row)
            if index_def.unique and any(value is not None for value in key) and index.lookup(key):
                raise _write_error("DUPLICATE_KEY", f"违反唯一索引：{index_def.name}")
            index.insert(key, record_id)
            self.objects.set_index_root(index_def.name, index.root_page)

    def _index_delete(self, schema, row, record_id):
        for index_def in self._index_defs(schema):
            index = self._index_tree(schema, index_def)
            index.delete(self._index_key(schema, index_def, row), record_id)
            self.objects.set_index_root(index_def.name, index.root_page)

    def _index_update(self, schema, old_row, new_row, old_record_id, new_record_id):
        for index_def in self._index_defs(schema):
            old_key = self._index_key(schema, index_def, old_row)
            new_key = self._index_key(schema, index_def, new_row)
            if old_key == new_key:
                continue
            index = self._index_tree(schema, index_def)
            index.delete(old_key, old_record_id)
            if index_def.unique and any(value is not None for value in new_key) and index.lookup(new_key):
                raise _write_error("DUPLICATE_KEY", f"违反唯一索引：{index_def.name}")
            index.insert(new_key, new_record_id)
            self.objects.set_index_root(index_def.name, index.root_page)

    def _rebuild_indexes(self, schema, old_index_defs=()):
        """结构变更后重建该表全部索引：丢弃旧树、按新结构重新构建。"""
        from minisql.storage.index import BTreeIndex
        name_to_column = {column.name: column for column in schema.columns}
        for index_def in old_index_defs:
            if index_def.root_page is not None and all(name in name_to_column for name in index_def.columns):
                columns = tuple(name_to_column[name] for name in index_def.columns)
                BTreeIndex(self.storage.pages, self.storage.buffer, columns,
                           index_def.root_page, schema.table_id).drop()
            self.objects.unregister_index(index_def.name)
        if not old_index_defs:
            return
        for index_def in old_index_defs:
            if not all(name in name_to_column for name in index_def.columns):
                continue  # 被变更移除的列上的索引不再重建
            self._build_index(schema, index_def.name, index_def.columns, index_def.unique)

    def _build_index(self, schema, name, columns, unique):
        """构建索引：B+ 树建树 + 存量扫描 + 登记 root_page（同事务）。"""
        from minisql.storage.index import BTreeIndex
        name_to_column = {column.name: column for column in schema.columns}
        key_columns = tuple(name_to_column[column] for column in columns)
        index = BTreeIndex.create(self.storage.pages, self.storage.buffer,
                                  key_columns, table_id=schema.table_id)
        name_to_index = {column.name: i for i, column in enumerate(schema.columns)}
        seen: set = set()
        for record in self.storage.scan(schema):
            key = tuple(record.row[name_to_index[column]] for column in columns)
            if unique and any(value is not None for value in key):
                if key in seen:
                    raise _write_error("DUPLICATE_KEY", f"存量数据违反唯一索引：{name}")
                seen.add(key)
            index.insert(key, record.record_id)
        from minisql.engine.objects import IndexDefinition
        self.objects.register_index(IndexDefinition(
            name, schema.name, tuple(columns), bool(unique), index.root_page))

    # ---------- DML ----------

    def _extended_insert(self, plan, outer=None):
        attributes = dict(plan.attributes)
        schema = self._table_schema(attributes["table"])
        names = attributes.get("assignments") or ()
        context = RowContext(RuntimeRow(), outer)  # 触发器动作经外层上下文取 NEW/OLD
        provided = {name: evaluate(expression, context)
                    for name, expression in zip(names, plan.expressions)}
        values = []
        for column in schema.columns:
            if column.name in provided:
                values.append(provided[column.name])
            else:
                values.append(self._default_value(schema, column))
        row = tuple(values)
        self._check_constraints(schema, row)
        record_id = self.storage.insert(schema, row)
        self._index_insert(schema, row, record_id)
        self.storage.flush()
        self._fire_triggers(schema, "INSERT", None, row)
        return ExecutionResult(affected_rows=1, message="已插入 1 行")

    def _extended_update(self, plan, outer=None):
        attributes = dict(plan.attributes)
        schema = self._table_schema(attributes["table"])
        names = attributes.get("assignments") or ()
        value_exprs = plan.expressions[:len(names)]
        predicate = plan.expressions[len(names)] if attributes.get("has_where") else None
        replacements = []
        for record in self.storage.scan(schema):
            context = RowContext(RuntimeRow(self._row_dict(plan, record.row)), outer)
            if predicate is not None and evaluate(predicate, context) is not True:
                continue
            row = list(record.row)
            name_to_index = {column.name: i for i, column in enumerate(schema.columns)}
            for name, expression in zip(names, value_exprs):
                row[name_to_index[name]] = evaluate(expression, context)
            replacements.append((record.record_id, tuple(record.row), tuple(row)))
        for old_record_id, old_row, new_row in replacements:
            self._check_constraints(schema, new_row, skip_row=old_row)
            self._check_parent_references(schema, old_row)
            new_record_id = self.storage.insert(schema, new_row)
            self.storage.delete(schema, old_record_id)
            self._index_update(schema, old_row, new_row, old_record_id, new_record_id)
        self.storage.flush()
        for _, old_row, new_row in replacements:  # 行级事件在语句自身写入完成后派发
            self._fire_triggers(schema, "UPDATE", old_row, new_row)
        return ExecutionResult(affected_rows=len(replacements), message=f"已更新 {len(replacements)} 行")

    def _extended_delete(self, plan, outer=None):
        attributes = dict(plan.attributes)
        schema = self._table_schema(attributes["table"])
        predicate = plan.expressions[0] if attributes.get("has_where") else None
        doomed = []
        for record in self.storage.scan(schema):
            if predicate is None:
                doomed.append(record)
                continue
            context = RowContext(RuntimeRow(self._row_dict(plan, record.row)), outer)
            if evaluate(predicate, context) is True:
                doomed.append(record)
        for record in doomed:
            self._check_parent_references(schema, record.row)
            self.storage.delete(schema, record.record_id)
            self._index_delete(schema, record.row, record.record_id)
        self.storage.flush()
        for record in doomed:  # 行级事件在语句自身写入完成后派发
            self._fire_triggers(schema, "DELETE", record.row, None)
        return ExecutionResult(affected_rows=len(doomed), message=f"已删除 {len(doomed)} 行")

    # ---------- 触发器 ----------

    def _extended_create_trigger(self, plan):
        from minisql.engine.objects import TriggerDefinition
        self._require_objects()
        attributes = dict(plan.attributes)
        definition = (getattr(self, "statement_text", "") or "").strip()
        if not definition.endswith(";"):
            definition += ";"
        self.objects.register_trigger(TriggerDefinition(
            attributes["trigger"], attributes["table"], attributes["event"].upper(),
            definition, self.objects.next_trigger_order()))
        self.storage.flush()
        return ExecutionResult(message=f"触发器 {attributes['trigger']} 已创建")

    def _compiled_trigger_actions(self, trigger):
        """重编译触发器定义取回动作计划（NEW/OLD 由编译器重新绑定）。"""
        cache = getattr(self, "_trigger_cache", None)
        if cache is None:
            cache = self._trigger_cache = {}
        if trigger.name not in cache:
            if self.compiler is None or self.catalog_view is None:
                raise _write_error("FEATURE_NOT_EXECUTABLE", "触发器动作缺少编译器，无法调度")
            from minisql.contracts.extensions import ExtendedPlan
            compiled = self.compiler.compile(
                trigger.action, _ReloadCatalogView(self.catalog_view, trigger.name.lower()))
            plans = [child for child in compiled.optimized_plan.children
                     if isinstance(child, ExtendedPlan)]
            cache[trigger.name] = plans
        return cache[trigger.name]

    def _fire_triggers(self, schema, event, old_row, new_row):
        """派发 AFTER 行级触发器：按创建先后执行，动态递归经活动栈拒绝。"""
        if self.objects is None:
            return
        from minisql.engine.executor import _collect_bindings
        for trigger in self.objects.get_triggers(schema.name, event):
            active = getattr(self, "_active_triggers", None)
            if active is None:
                active = self._active_triggers = set()
            if trigger.name in active:
                raise _write_error("RECURSIVE_TRIGGER", f"触发器动态递归被拒绝：{trigger.name}")
            active.add(trigger.name)
            try:
                for action in self._compiled_trigger_actions(trigger):
                    values = {}
                    for key, binding in _collect_bindings(action).items():
                        qualifier = (binding.qualifier or "").lower()
                        if qualifier == "new" and new_row is not None:
                            values[key] = tuple(new_row)
                        elif qualifier == "old" and old_row is not None:
                            values[key] = tuple(old_row)
                    self._run_trigger_action(action, RowContext(RuntimeRow(values)))
            finally:
                active.discard(trigger.name)

    def _run_trigger_action(self, action, context):
        """执行一个触发器动作；SELECT 动作按约定丢弃结果。

        触发器按调用者权限执行（契约）：动作涉及的表同样走统一权限检查。"""
        self._enforce_plan_permissions(action)
        operator = action.operator
        if operator == "Insert":
            self._extended_insert(action, context)
        elif operator == "Update":
            self._extended_update(action, context)
        elif operator == "Delete":
            self._extended_delete(action, context)
        elif operator in ("ExpressionProject", "Filter", "TableScan", "Sort", "Limit", "Distinct"):
            cursor = _ScanCursor(self._scan_assignments(action))
            self._extended_node(action, context, cursor)  # 结果按 discard_select_results 丢弃
        else:
            raise _write_error("FEATURE_NOT_EXECUTABLE", f"触发器动作 {operator} 的执行尚未接入")

    # ---------- DDL ----------

    def _extended_create_table(self, plan):
        from minisql.engine.objects import ConstraintDefinition
        self._require_objects()
        attributes = dict(plan.attributes)
        schema = attributes["schema"]
        assigned = self.storage.create_table(schema)
        self.catalog.register_table(assigned)
        for position, column in enumerate(schema.columns):
            if not column.nullable:
                self.objects.register_constraint(ConstraintDefinition(
                    schema.name, f"__notnull_{column.name}", "NOT NULL", (column.name,)))
            if column.has_default and column.default is not None:
                self.objects.register_constraint(ConstraintDefinition(
                    schema.name, f"__default_{column.name}", "DEFAULT", (column.name,),
                    default_text=serialize_expr(column.default)))
        for index, constraint in enumerate(schema.constraints or ()):
            name = constraint.name or f"__constraint_{index}"
            expression = serialize_expr(constraint.expression) if constraint.expression is not None else ""
            self.objects.register_constraint(ConstraintDefinition(
                schema.name, name, constraint.kind, constraint.columns,
                expression=expression,
                reference_table=constraint.reference_table or "",
                reference_columns=constraint.reference_columns or ()))
        self.storage.flush()
        return ExecutionResult(message=f"表 {assigned.name} 已创建")

    def _extended_alter_table(self, plan):
        self._require_objects()
        attributes = dict(plan.attributes)
        old_schema = self._table_schema(attributes["table"])
        new_schema = attributes["schema"]
        action = attributes.get("action") or ""
        old_columns = [column.name for column in old_schema.columns]
        new_columns = [column.name for column in new_schema.columns]

        def transform(row):
            if action == "ADD COLUMN":
                column = new_schema.columns[-1]
                if column.has_default and column.default is not None:
                    value = evaluate(column.default, RowContext(RuntimeRow()))
                else:
                    value = None
                return tuple(row) + (value,)
            if action == "DROP COLUMN":
                dropped = next(name for name in old_columns if name not in new_columns)
                position = old_columns.index(dropped)
                return tuple(value for index, value in enumerate(row) if index != position)
            # RENAME COLUMN / RENAME TO / ALTER COLUMN TYPE：值按位置保持（类型转换在编码时校验）
            return tuple(row)

        old_index_defs = self._index_defs(old_schema)
        added_columns = [column for column in new_schema.columns
                         if column.name not in old_columns]
        assigned = self.storage.rewrite_table(old_schema, new_schema, transform)
        self.catalog.unregister_table(old_schema.name)
        self.catalog.register_table(assigned)
        # 新增列的非空/默认值登记持久化（供后续写入与目录补全使用）
        from minisql.engine.objects import ConstraintDefinition
        for column in added_columns:
            if not column.nullable:
                self.objects.register_constraint(ConstraintDefinition(
                    new_schema.name, f"__notnull_{column.name}", "NOT NULL", (column.name,)))
            if column.has_default and column.default is not None:
                self.objects.register_constraint(ConstraintDefinition(
                    new_schema.name, f"__default_{column.name}", "DEFAULT", (column.name,),
                    default_text=serialize_expr(column.default)))
        # 约束维护：被删除/重命名的列上的约束移除（保守，不自动改写 CHECK）
        for constraint in self.objects.get_constraints(old_schema.name):
            missing = [name for name in constraint.columns
                       if name not in new_columns]
            if missing:
                self.objects.unregister_constraints(old_schema.name, constraint.name)
        self._rebuild_indexes(assigned, old_index_defs)
        self.storage.flush()
        return ExecutionResult(message=f"表 {old_schema.name} 已完成 {action}")

    def _extended_create_index(self, plan):
        self._require_objects()
        attributes = dict(plan.attributes)
        schema = self._table_schema(attributes["table"])
        columns = attributes["columns"]
        for name in columns:
            if name not in [column.name for column in schema.columns]:
                raise _write_error("UNKNOWN_COLUMN", name)
        self._build_index(schema, attributes["index"], columns, bool(attributes.get("unique")))
        self.storage.flush()
        return ExecutionResult(message=f"索引 {attributes['index']} 已创建")

    def _extended_drop_object(self, plan):
        self._require_objects()
        operator = plan.operator
        attributes = dict(plan.attributes)
        if operator == "DropView":
            name = attributes.get("view") or attributes.get("name")
            self.objects.assert_droppable("view", name)
            view = self.objects.get_view(name)
            if view is None:
                raise _write_error("UNKNOWN_OBJECT", f"视图 {name} 不存在")
            self.objects.remove_object("view", name)
            self.objects.unregister_view(name)
        elif operator == "DropIndex":
            name = attributes.get("index") or attributes.get("name")
            index = self.objects.get_index(name)
            if index is None:
                raise _write_error("UNKNOWN_OBJECT", f"索引 {name} 不存在")
            schema = self._table_schema(index.table)
            from minisql.storage.index import BTreeIndex
            name_to_column = {column.name: column for column in schema.columns}
            if index.root_page is not None and all(n in name_to_column for n in index.columns):
                columns = tuple(name_to_column[n] for n in index.columns)
                BTreeIndex(self.storage.pages, self.storage.buffer, columns,
                           index.root_page, schema.table_id).drop()
            self.objects.unregister_index(name)
        elif operator == "DropTrigger":
            name = attributes.get("trigger") or attributes.get("name")
            trigger = self.objects.get_trigger(name)
            if trigger is None:
                raise _write_error("UNKNOWN_OBJECT", f"触发器 {name} 不存在")
            self.objects.unregister_trigger(name)
        else:
            raise _write_error("FEATURE_NOT_EXECUTABLE", f"{operator} 的执行尚未接入")
        self.storage.flush()
        return ExecutionResult(message=f"{operator[4:]} 已删除")

    def _extended_create_view(self, plan):
        self._require_objects()
        from minisql.engine.objects import ViewDefinition
        attributes = dict(plan.attributes)
        name = attributes["view"]
        definition = self._view_definition_text(name)
        output = plan.output
        columns = tuple(
            __import__("minisql.contracts.models", fromlist=["ColumnSchema"]).ColumnSchema(
                field.name, DataType(field.type.kind))
            for field in output)
        self.objects.register_view(ViewDefinition(name, definition, columns))
        for dependency in plan.dependencies:
            self.objects.add_dependency("view", name, dependency.kind, dependency.name)
        self.storage.flush()
        return ExecutionResult(message=f"视图 {name} 已创建")

    def _view_definition_text(self, name):
        """从当前语句文本中提取视图查询体（CREATE VIEW <name> AS <query>）。"""
        import re
        text = (getattr(self, "statement_text", "") or "").strip().rstrip(";")
        match = re.match(r"(?is)^\s*CREATE\s+VIEW\s+\S+\s+AS\s+(.*)$", text)
        body = match.group(1).strip() if match else text
        return body if body.endswith(";") else body + ";"

    # ---------- 账户与授权 ----------

    def _extended_user_command(self, plan):
        if self.accounts is None:
            raise _write_error("FEATURE_NOT_EXECUTABLE", "账户存储未接入")
        operator = plan.operator
        attributes = dict(plan.attributes)
        name = attributes.get("user")
        if operator == "CreateUser":
            if plan.password is None:
                raise _write_error("INVALID_PASSWORD", "创建账户必须提供密码")
            if self._enforcing():
                # 已有账户时必须由管理员创建；首个账户视为初始化管理员。
                self.accounts.require_admin(getattr(self, "session", None))
                self.accounts.create_account(name, plan.password)
            else:
                self.accounts.create_account(name, plan.password, is_admin=True)
            return ExecutionResult(message=f"账户 {name} 已创建")
        if operator == "DropUser":
            self.accounts.require_admin(getattr(self, "session", None))
            self.accounts.remove_account(getattr(self, "session", None), name)
            return ExecutionResult(message=f"账户 {name} 已删除")
        raise _write_error("FEATURE_NOT_EXECUTABLE", f"{operator} 的执行尚未接入")

    def _extended_authorization(self, plan):
        if self.accounts is None:
            raise _write_error("FEATURE_NOT_EXECUTABLE", "账户存储未接入")
        if self._enforcing():
            self.accounts.require_admin(getattr(self, "session", None))
        attributes = dict(plan.attributes)
        user = attributes["user"]
        object_kind = attributes.get("object_kind") or "table"
        object_name = attributes.get("object") or ""
        permissions = attributes.get("permissions") or ()
        for permission in permissions:
            if plan.operator == "Grant":
                self.accounts.grant(user, permission, object_kind, object_name)
            elif plan.operator == "Revoke":
                self.accounts.revoke(user, permission, object_kind, object_name)
            else:
                raise _write_error("FEATURE_NOT_EXECUTABLE", f"{plan.operator} 的执行尚未接入")
        self.storage.flush()
        return ExecutionResult(
            message=f"已{'授权' if plan.operator == 'Grant' else '撤权'}：{user}")
