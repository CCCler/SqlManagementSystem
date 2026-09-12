"""PlanExecutor：执行 CreateTable/Insert/SeqScan/Filter/Project/Delete/Explain 计划。

SeqScan/Filter 保留 StoredRecord 的 RecordId；SELECT 的 Project 产生结果列；
DELETE 只接收扫描或过滤结果，禁止 Project 以保留 RecordId；
EXPLAIN 仅渲染计划树，不扫描、不修改数据。"""
from minisql.contracts.ast import BinaryExpr, Expression, Identifier, Literal, UnaryExpr
from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.interfaces import CatalogWriter, RecordStorage
from minisql.contracts.models import ExecutionResult, Row, StoredRecord, Value
from minisql.contracts.plans import (
    Update, CreateTable, Delete, DropTable, EmptyScan, Explain, Filter, Insert, Plan, Project,
    QueryPlan, SeqScan, TransactionControl,
)
from minisql.engine.expr import (
    Row as RuntimeRow, RowContext, aggregate_value, boolean_values, evaluate, expr_key,
)
from minisql.engine.write_path import WritePathMixin

# 扩展 DML/DDL 算子 → 写路径处理器（阶段 C/D/E）。
_EXTENDED_WRITE_HANDLERS = {
    "Insert": "_extended_insert",
    "Update": "_extended_update",
    "Delete": "_extended_delete",
    "CreateTable": "_extended_create_table",
    "AlterTable": "_extended_alter_table",
    "CreateIndex": "_extended_create_index",
    "CreateView": "_extended_create_view",
    "CreateTrigger": "_extended_create_trigger",
    "DropView": "_extended_drop_object",
    "DropIndex": "_extended_drop_object",
    "DropTrigger": "_extended_drop_object",
    "CreateUser": "_extended_user_command",
    "DropUser": "_extended_user_command",
    "AlterUser": "_extended_user_command",
    "Grant": "_extended_authorization",
    "Revoke": "_extended_authorization",
}

# 阶段 A/B 已接入执行器的扩展查询算子；其余算子报 FEATURE_NOT_EXECUTABLE。
EXTENDED_QUERY_OPERATORS = (
    "TableScan", "Filter", "ExpressionProject", "Sort", "Limit", "Distinct",
    "Aggregate", "Having", "Join", "SetOperation", "DerivedTable", "ViewScan",
    "IndexScan",
)

INT_MIN = -(2 ** 63)
INT_MAX = 2 ** 63 - 1


def _execution_error(code: str, reason: str) -> MiniSQLError:
    return MiniSQLError(ErrorStage.EXECUTION, code, reason)


def _require_int(value: Value, context: str) -> None:
    """bool 不得因 Python 的继承关系被当成 INT。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise _execution_error("TYPE_MISMATCH", f"{context} 需要 INT")


def _query_columns(plan: QueryPlan) -> tuple[str, ...]:
    if isinstance(plan, SeqScan):
        return tuple(column.name for column in plan.schema.columns)
    if isinstance(plan, Filter):
        return _query_columns(plan.source)
    if isinstance(plan, (Project, EmptyScan)):
        return plan.columns
    raise _execution_error("UNKNOWN_PLAN", f"未知查询计划: {type(plan).__name__}")


def _collect_bindings(plan) -> dict:
    """收集计划表达式引用的绑定键 → FieldBinding（每键取首个，不进入子查询计划）。"""
    from minisql.contracts.extensions import Expr, ExtendedPlan
    bindings: dict = {}

    def visit_expr(expression):
        binding = getattr(expression, "binding", None)
        if binding is not None:
            bindings.setdefault((binding.scope, binding.source), binding)
        for arg in expression.args:
            if isinstance(arg, Expr):
                visit_expr(arg)
            elif isinstance(arg, (tuple, list)):
                for item in arg:
                    if isinstance(item, Expr):
                        visit_expr(item)

    def visit_value(value):
        if isinstance(value, Expr):
            visit_expr(value)
        elif isinstance(value, (tuple, list)):
            for item in value:
                visit_value(item)

    def visit_plan(node):
        for expression in getattr(node, "expressions", ()):
            visit_expr(expression)
        for value in dict(getattr(node, "attributes", ()) or ()).values():
            visit_value(value)
        for child in getattr(node, "children", ()):
            if isinstance(child, ExtendedPlan):
                visit_plan(child)

    visit_plan(plan)
    return bindings


class _ScanCursor:
    """按叶子出现顺序分发 (绑定键, 列数)；Join 两侧据此识别各自的来源。"""

    __slots__ = ("assignments", "index")

    def __init__(self, assignments):
        self.assignments = assignments
        self.index = 0

    def take(self):
        item = self.assignments[self.index] if self.index < len(self.assignments) else (None, 0)
        self.index += 1
        return item


def _dedupe(values: list) -> list:
    """保序去重；NULL 与 NULL 视为相同（集合语义与分组一致）。"""
    seen: set = set()
    unique = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _source_nodes(plan) -> list:
    """按左到右深度优先列出来源节点（TableScan/IndexScan/DerivedTable/ViewScan）。

    DerivedTable/ViewScan 视作本查询块的来源叶子，其内部是独立查询块，
    执行时另外分配绑定键。"""
    from minisql.contracts.extensions import ExtendedPlan
    if plan.operator in ("TableScan", "IndexScan", "DerivedTable", "ViewScan"):
        return [plan]
    nodes = []
    for child in plan.children:
        if isinstance(child, ExtendedPlan):
            nodes.extend(_source_nodes(child))
    return nodes


def _scan_leaves(plan) -> list:
    """按左到右深度优先列出 TableScan 叶子（与编译器 source 编号顺序一致）。"""
    from minisql.contracts.extensions import ExtendedPlan
    if plan.operator == "TableScan":
        return [plan]
    leaves = []
    for child in plan.children:
        if isinstance(child, ExtendedPlan):
            leaves.extend(_scan_leaves(child))
    return leaves


def _coerce_index_bound(column, value, direction):
    """索引边界与列类型对齐；不精确时放宽为超集（上层 Filter 做精确残余检查）。"""
    from decimal import Decimal
    if value is None or column is None:
        return value
    kind = column.data_type.value
    if kind == "DECIMAL":
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return Decimal(value)  # 精确提升
        return value
    if kind == "INT" and isinstance(value, Decimal):
        import math
        if direction == "hi":
            return math.ceil(value)          # 上界放宽
        if direction == "lo":
            return math.floor(value)         # 下界放宽
        return int(value) if value == int(value) else math.floor(value)
    return value


def _sort_pass(rows, expression, descending, outer, output_keys):
    """单键稳定排序：DESC 时 NULL 在前，ASC 时 NULL 在后。"""
    def key_of(row):
        key = expr_key(expression)
        if output_keys and key in output_keys:
            return row.output[output_keys.index(key)]
        return evaluate(expression, RowContext(row, outer))

    valued = [(key_of(row), row) for row in rows]
    nulls = [item for item in valued if item[0] is None]
    non_nulls = [item for item in valued if item[0] is not None]
    non_nulls.sort(key=lambda item: item[0], reverse=descending)
    ordered = (nulls + non_nulls) if descending else (non_nulls + nulls)
    return [row for _, row in ordered]


def _render_expr(expression: Expression) -> str:
    if isinstance(expression, Literal):
        value = expression.value
        return "'" + value.replace("'", "''") + "'" if isinstance(value, str) else repr(value)
    if isinstance(expression, Identifier):
        return expression.name
    if isinstance(expression, UnaryExpr):
        return f"({expression.operator} {_render_expr(expression.operand)})"
    if isinstance(expression, BinaryExpr):
        return f"({_render_expr(expression.left)} {expression.operator} {_render_expr(expression.right)})"
    return str(expression)


def _indent(text: str) -> str:
    return "\n".join("  " + line for line in text.split("\n"))


def render_plan(plan: Plan) -> str:
    """把计划渲染为可读的缩进树；供 EXPLAIN 只读展示，不执行任何算子。"""
    from minisql.contracts.extensions import ExtendedPlan
    if isinstance(plan, ExtendedPlan):
        from minisql.cli.trace import to_json_value
        import json
        from minisql.compiler.extended_optimizer import collect_capabilities
        return "编译计划（仅展示，不执行；所需能力：" + ", ".join(collect_capabilities(plan)) + "）\n" + json.dumps(to_json_value(plan), ensure_ascii=False, indent=2)
    if isinstance(plan, Explain):
        return render_plan(plan.plan)
    if isinstance(plan, SeqScan):
        return f"SeqScan({plan.schema.name})"
    if isinstance(plan, EmptyScan):
        return f"EmptyScan({', '.join(plan.columns)})"
    if isinstance(plan, Filter):
        return f"Filter({_render_expr(plan.predicate)})\n" + _indent(render_plan(plan.source))
    if isinstance(plan, Project):
        line = f"Project({', '.join(plan.columns)})"
        if plan.distinct:
            line += " DISTINCT"
        if plan.order_by:
            terms = ", ".join(f"{name} {'DESC' if descending else 'ASC'}" for name, descending in plan.order_by)
            line += f" ORDER BY {terms}"
        if plan.limit is not None:
            line += f" LIMIT {plan.limit}"
        if plan.offset is not None:
            line += f" OFFSET {plan.offset}"
        return line + "\n" + _indent(render_plan(plan.source))
    if isinstance(plan, Update):
        assignments = ", ".join(f"{name} = {_render_expr(value)}" for name, value in plan.assignments)
        return f"Update({plan.schema.name}, {assignments})\n" + _indent(render_plan(plan.source))
    if isinstance(plan, Delete):
        return f"Delete({plan.schema.name})\n" + _indent(render_plan(plan.source))
    if isinstance(plan, CreateTable):
        return f"CreateTable({plan.schema.name})"
    if isinstance(plan, Insert):
        return f"Insert({plan.schema.name})"
    if isinstance(plan, DropTable):
        return f"DropTable({plan.schema.name})"
    if isinstance(plan, TransactionControl):
        return f"TransactionControl({plan.action})"
    return str(plan)


class PlanExecutor(WritePathMixin):
    def __init__(self, storage: RecordStorage, catalog: CatalogWriter) -> None:
        self.storage = storage
        self.catalog = catalog
        # 由 Database 注入的可选依赖：对象目录、账户存储、编译器与当前语句文本。
        self.objects = None
        self.accounts = None
        self.compiler = None
        self.catalog_view = None
        self.session = None
        self.statement_text = ""

    def execute(self, plan: Plan) -> ExecutionResult:
        from minisql.contracts.extensions import ExtendedPlan
        self._enforce_plan_permissions(plan)  # 统一入口鉴权（初始化模式跳过）
        if isinstance(plan, ExtendedPlan):
            return self._execute_extended(plan)
        if not isinstance(plan, Explain):
            from minisql.compiler.capabilities import require_legacy_plan
            require_legacy_plan(plan)
        if isinstance(plan, Explain):
            return ExecutionResult(message=render_plan(plan.plan))
        if isinstance(plan, CreateTable):
            return self._create_table(plan)
        if isinstance(plan, DropTable):
            return self._drop_table(plan)
        if isinstance(plan, Insert):
            return self._insert(plan)
        if isinstance(plan, Update):
            return self._update(plan)
        if isinstance(plan, Delete):
            return self._delete(plan)
        if isinstance(plan, (SeqScan, Filter, Project, EmptyScan)):
            columns, rows = self._run_query(plan)
            return ExecutionResult(columns=columns, rows=tuple(rows))
        raise _execution_error("UNKNOWN_PLAN", f"不支持的计划类型: {type(plan).__name__}")

    # ---------- 扩展计划执行（阶段 A：单表查询算子） ----------

    def _execute_extended(self, plan) -> ExecutionResult:
        """执行扩展计划；未接入的算子保持 FEATURE_NOT_EXECUTABLE 屏障。"""
        if plan.operator == "Explain":
            return ExecutionResult(message=render_plan(plan.children[0]))
        handler = _EXTENDED_WRITE_HANDLERS.get(plan.operator)
        if handler is not None:
            return getattr(self, handler)(plan)
        if plan.operator not in EXTENDED_QUERY_OPERATORS:
            raise _execution_error("FEATURE_NOT_EXECUTABLE",
                                   f"扩展算子 {plan.operator} 的执行尚未接入")
        cursor = _ScanCursor(self._scan_assignments(plan))
        rows, output, _ = self._extended_node(plan, None, cursor)
        result_rows = tuple(
            row.output if row.output is not None else tuple(next(iter(row.values.values()), ()))
            for row in rows
        )
        return ExecutionResult(columns=tuple(field.name for field in output), rows=result_rows)

    def _scan_assignments(self, plan):
        """按叶子顺序为每个来源叶子匹配 (scope, source) 绑定键。

        以 FieldBinding 的限定名 + 列名 + 序号三元组对准来源表结构（自连接靠
        别名区分）；无歧义时可退化按列名与序号匹配。未匹配的绑定属于外层
        作用域（相关子查询），由运行期上下文链回溯解析。"""
        bindings = _collect_bindings(plan)
        assignments = []
        used: set = set()
        for source in _source_nodes(plan):
            attributes = dict(source.attributes)
            if source.operator in ("TableScan", "IndexScan"):
                table_name = attributes["table"]
                alias = (attributes.get("alias") or table_name).lower()
                schema = self.catalog.get_table(table_name)
                names = [column.name for column in schema.columns] if schema is not None else []
            else:  # DerivedTable / ViewScan：本查询块的来源，其内部是独立查询块
                alias = (attributes.get("alias") or attributes.get("view") or "").lower()
                names = [field.name for field in source.output]
            key = None
            for candidate, binding in bindings.items():
                if (candidate in used or binding.ordinal >= len(names)
                        or names[binding.ordinal] != binding.name):
                    continue
                if (binding.qualifier or "").lower() == alias:
                    key = candidate
                    break
            if key is None:
                candidates = [candidate for candidate, binding in bindings.items()
                              if candidate not in used and binding.ordinal < len(names)
                              and names[binding.ordinal] == binding.name]
                if len(candidates) == 1:
                    key = candidates[0]
            if key is not None:
                used.add(key)
            assignments.append((key, len(names)))
        return assignments

    def _extended_node(self, plan, outer, cursor):
        """递归执行一个扩展算子，返回 (rows, output, output_keys)。"""
        from minisql.contracts.extensions import ExtendedPlan
        operator = plan.operator
        if operator == "TableScan":
            attributes = dict(plan.attributes)
            schema = self.catalog.get_table(attributes["table"])
            if schema is None:
                raise _execution_error("UNKNOWN_TABLE", attributes["table"])
            values_key, _ = cursor.take()
            rows = [
                RuntimeRow({values_key: tuple(record.row)} if values_key else {})
                for record in self.storage.scan(schema)
            ]
            return rows, plan.output, []
        if operator == "IndexScan":
            return self._index_scan_rows(plan, cursor)
        if operator == "Join":
            return self._join_rows(plan, outer, cursor)
        if operator in ("DerivedTable", "ViewScan"):
            values_key, _ = cursor.take()
            child = plan.children[0]
            sub_cursor = _ScanCursor(self._scan_assignments(child))
            rows, _, _ = self._extended_node(child, None, sub_cursor)
            rebound = []
            for row in rows:
                output = row.output if row.output is not None else ()
                rebound.append(RuntimeRow({values_key: output} if values_key else {}, output))
            return rebound, plan.output, []
        if operator == "SetOperation":
            return self._set_operation_rows(plan, outer, cursor)
        if operator not in ("Filter", "Having", "ExpressionProject", "Sort", "Limit",
                            "Distinct", "Aggregate"):
            raise _execution_error("FEATURE_NOT_EXECUTABLE",
                                   f"扩展算子 {operator} 的执行尚未接入")
        child = plan.children[0]
        if not isinstance(child, ExtendedPlan):
            raise _execution_error("FEATURE_NOT_EXECUTABLE", "扩展算子缺少子计划")
        rows, output, output_keys = self._extended_node(child, outer, cursor)
        if operator in ("Filter", "Having"):
            predicate = plan.expressions[0]
            rows = [row for row in rows
                    if evaluate(predicate, self._row_context(row, outer, output_keys)) is True]
            return rows, output, output_keys
        if operator == "Aggregate":
            return self._aggregate_rows(plan, rows, outer)
        if operator == "ExpressionProject":
            items = plan.expressions
            child_keys = output_keys
            new_keys = [expr_key(item) for item in items]
            for row in rows:
                context = self._row_context(row, outer, child_keys)
                row.output = tuple(evaluate(item, context) for item in items)
            return rows, plan.output, new_keys
        if operator == "Sort":
            return self._sort_rows(plan, rows, outer, output_keys), output, output_keys
        if operator == "Limit":
            attributes = dict(plan.attributes)
            offset = attributes.get("offset") or 0
            limit = attributes.get("limit")
            end = None if limit is None else offset + limit
            return rows[offset:end], output, output_keys
        seen: set = set()  # Distinct
        unique = []
        for row in rows:
            if row.output not in seen:
                seen.add(row.output)
                unique.append(row)
        return unique, output, output_keys

    def _index_scan_rows(self, plan, cursor):
        """索引扫描：按 bounds 求 lo/hi 后 range_scan 取候选 rid，再回表取行。

        索引计划保留完整 Filter 做残余检查，因此这里只需返回候选超集。"""
        attributes = dict(plan.attributes)
        schema = self._table_schema_for_scan(attributes["table"])
        values_key, _ = cursor.take()
        index_def = self.objects.get_index(attributes["index"]) if self.objects else None
        if index_def is None or index_def.root_page is None:
            raise _execution_error("FEATURE_NOT_EXECUTABLE", f"索引 {attributes.get('index')} 不可用")
        index = self._index_tree(schema, index_def)
        key_columns = {column.name: column for column in schema.columns}
        lo: list = []
        hi: list = []
        lo_inclusive = hi_inclusive = True
        for column_name, operator, literal_expr in attributes.get("bounds") or ():
            value = evaluate(literal_expr, RowContext(RuntimeRow()))
            column = key_columns.get(column_name)
            if operator == "=":
                lo.append(_coerce_index_bound(column, value, "="))
                hi.append(_coerce_index_bound(column, value, "="))
            elif operator in (">", ">="):
                lo.append(_coerce_index_bound(column, value, "lo"))
                lo_inclusive = operator == ">="
                break
            else:
                hi.append(_coerce_index_bound(column, value, "hi"))
                hi_inclusive = operator == "<="
                break
        record_ids = index.range_scan(tuple(lo) or None, tuple(hi) or None,
                                      lo_inclusive, hi_inclusive)
        by_id = {record.record_id: tuple(record.row) for record in self.storage.scan(schema)}
        rows = [RuntimeRow({values_key: by_id[rid]} if values_key else {})
                for rid in record_ids if rid in by_id]
        return rows, plan.output, []

    def _table_schema_for_scan(self, name):
        schema = self.catalog.get_table(name)
        if schema is None:
            raise _execution_error("UNKNOWN_TABLE", name)
        return schema

    def _join_rows(self, plan, outer, cursor):
        """四类连接：INNER/CROSS 笛卡尔积过滤；LEFT/RIGHT 缺失侧补 NULL。"""
        kind = (dict(plan.attributes).get("kind") or "INNER").upper()
        left_plan, right_plan = plan.children[0], plan.children[1]
        left_start = cursor.index
        left_rows, output, _ = self._extended_node(left_plan, outer, cursor)
        right_start = cursor.index
        right_rows, _, _ = self._extended_node(right_plan, outer, cursor)
        left_bindings = cursor.assignments[left_start:right_start]
        right_bindings = cursor.assignments[right_start:cursor.index]

        def merge(left_row, right_row):
            values = dict(left_row.values)
            values.update(right_row.values)
            return RuntimeRow(values)

        def matched(left_row, right_row):
            if not plan.expressions:  # CROSS：无 ON
                return True
            return evaluate(plan.expressions[0],
                            self._row_context(merge(left_row, right_row), outer, [])) is True

        result = []
        if kind == "RIGHT":
            null_left = {key: (None,) * size for key, size in left_bindings if key}
            for right_row in right_rows:
                found = False
                for left_row in left_rows:
                    if matched(left_row, right_row):
                        result.append(merge(left_row, right_row))
                        found = True
                if not found:
                    values = dict(null_left)
                    values.update(right_row.values)
                    result.append(RuntimeRow(values))
        else:
            null_right = {key: (None,) * size for key, size in right_bindings if key}
            for left_row in left_rows:
                found = False
                for right_row in right_rows:
                    if matched(left_row, right_row):
                        result.append(merge(left_row, right_row))
                        found = True
                if kind == "LEFT" and not found:
                    values = dict(left_row.values)
                    values.update(null_right)
                    result.append(RuntimeRow(values))
        return result, output, []

    def _set_operation_rows(self, plan, outer, cursor):
        """集合操作：UNION 去重、UNION ALL 保序全留、INTERSECT/EXCEPT 集合语义。"""
        kind = (dict(plan.attributes).get("kind") or "").upper()
        left_rows, output, _ = self._extended_node(plan.children[0], outer, cursor)
        right_rows, _, _ = self._extended_node(plan.children[1], outer, cursor)

        def values_of(row):
            return row.output if row.output is not None else tuple(next(iter(row.values.values()), ()))

        left_values = [values_of(row) for row in left_rows]
        right_values = [values_of(row) for row in right_rows]
        right_set = set(right_values)
        if kind == "UNION ALL":
            combined = left_values + right_values
        elif kind == "UNION":
            combined = _dedupe(left_values + right_values)
        elif kind == "INTERSECT":
            combined = _dedupe([value for value in left_values if value in right_set])
        elif kind == "EXCEPT":
            combined = _dedupe([value for value in left_values if value not in right_set])
        else:
            raise _execution_error("FEATURE_NOT_EXECUTABLE", f"集合操作 {kind} 的执行尚未接入")
        return [RuntimeRow(output=value) for value in combined], plan.output, []

    def _run_subquery_values(self, plan, context):
        """子查询执行器：以给定上下文为外层，返回投影值元组列表（相关绑定回溯）。"""
        cursor = _ScanCursor(self._scan_assignments(plan))
        rows, _, _ = self._extended_node(plan, context, cursor)
        return [
            row.output if row.output is not None else tuple(next(iter(row.values.values()), ()))
            for row in rows
        ]

    def _row_context(self, row, outer, output_keys):
        """构造求值上下文：投影后的行按 expr_key 取已算好的投影/聚合值。"""
        mapped = None
        if row.output is not None and output_keys:
            mapped = {key: value for key, value in zip(output_keys, row.output)}
        runner = outer.runner if outer is not None and outer.runner is not None else self._run_subquery_values
        return RowContext(row, outer, mapped, runner)

    def _aggregate_rows(self, plan, rows, outer):
        """分组聚合：groups 为零时全部行为一组（空输入产出 COUNT=0 行）。

        聚合清单取 attrs['aggregates']（编译器显式给出）；plan.expressions 中
        分组键之后的其余项是投影表达式，不在此求值。"""
        attributes = dict(plan.attributes)
        group_count = attributes.get("group_count") or 0
        group_exprs = plan.expressions[:group_count]
        aggregate_exprs = tuple(attributes.get("aggregates") or (
            expression for expression in plan.expressions[group_count:]
            if expression.op in ("COUNT", "SUM", "AVG", "MAX", "MIN")
        ))
        buckets: dict = {}
        if not rows and group_count == 0:
            buckets[()] = []
        for row in rows:
            context = self._row_context(row, outer, [])
            key = tuple(evaluate(expression, context) for expression in group_exprs)
            buckets.setdefault(key, []).append(row)
        output_keys = [expr_key(expression) for expression in group_exprs + aggregate_exprs]
        result = []
        for key, members in buckets.items():
            values = list(key) + [
                aggregate_value(expression, members, outer) for expression in aggregate_exprs
            ]
            result.append(RuntimeRow(values={}, output=tuple(values)))
        return result, plan.output, output_keys

    def _sort_rows(self, plan, rows, outer, output_keys):
        """多列稳定排序；ASC 默认 NULL LAST、DESC 默认 NULL FIRST（编译器固定契约）。"""
        attributes = dict(plan.attributes)
        descending = tuple(attributes.get("descending") or ())
        ordered = list(rows)
        for index in range(len(plan.expressions) - 1, -1, -1):
            expression = plan.expressions[index]
            is_descending = descending[index] if index < len(descending) else False
            ordered = _sort_pass(ordered, expression, is_descending, outer, output_keys)
        return ordered

    def _create_table(self, plan: CreateTable) -> ExecutionResult:
        # 先分配物理结构，成功后登记 Catalog；登记失败不写目录。
        assigned = self.storage.create_table(plan.schema)
        self.catalog.register_table(assigned)
        self.storage.flush()
        return ExecutionResult(message=f"表 {assigned.name} 已创建")

    def _insert(self, plan: Insert) -> ExecutionResult:
        self.storage.insert(plan.schema, plan.values)
        self.storage.flush()
        self._fire_triggers(plan.schema, "INSERT", None, plan.values)
        return ExecutionResult(affected_rows=1, message="已插入 1 行")

    def _drop_table(self, plan: DropTable) -> ExecutionResult:
        if plan.schema.table_id == 0 or plan.schema.name.lower() == "__catalog":
            raise _execution_error("PROTECTED_TABLE", "不能删除系统目录表")
        # 防止旧计划误删同名重建后的新表。
        current = self.catalog.get_table(plan.schema.name)
        if current is None or current.table_id != plan.schema.table_id:
            raise _execution_error("UNKNOWN_TABLE", plan.schema.name)
        if self.objects is not None:
            # 依赖保护：视图/触发器等对象引用该表时保守拒绝（首版不隐式级联）。
            self.objects.assert_droppable("table", current.name)
        self.storage.drop_table(current)
        self.catalog.unregister_table(current.name)
        self.storage.flush()
        return ExecutionResult(message=f"表 {current.name} 已删除")

    def _update(self, plan: Update) -> ExecutionResult:
        if not isinstance(plan.source, (SeqScan, Filter, EmptyScan)):
            raise _execution_error("UNKNOWN_PLAN", "UPDATE 源必须保留 RecordId")
        if plan.schema.table_id == 0 or plan.schema.name.lower() == "__catalog":
            raise _execution_error("PROTECTED_TABLE", "不能更新系统目录表")
        indexes = {c.name: i for i, c in enumerate(plan.schema.columns)}
        replacements = []
        # 固定原记录集合，在写入前完成求值；多列赋值始终读取旧行。
        for record in self._scan_records(plan.source):
            row = list(record.row)
            for name, expression in plan.assignments:
                row[indexes[name]] = self._eval(expression, record.row, indexes)
            replacements.append((record.record_id, tuple(row)))
        originals = {record.record_id: record.row for record in self._scan_records(plan.source)}
        for record_id, row in replacements:
            # 复用堆存储支持变长记录迁移；事务日志负责整条语句的失败恢复。
            # 先插入再删除，不改变尚未更新记录的槽编号。
            self.storage.insert(plan.schema, row)
            self.storage.delete(plan.schema, record_id)
        self.storage.flush()
        for record_id, row in replacements:  # 行级事件在语句自身写入完成后派发
            self._fire_triggers(plan.schema, "UPDATE", originals.get(record_id), row)
        count = len(replacements)
        return ExecutionResult(affected_rows=count, message=f"已更新 {count} 行")

    def _delete(self, plan: Delete) -> ExecutionResult:
        if not isinstance(plan.source, (SeqScan, Filter, EmptyScan)):
            raise _execution_error("UNKNOWN_PLAN", "DELETE 只允许 SeqScan/Filter/EmptyScan 源以保留 RecordId")
        doomed = self._scan_records(plan.source)
        for record in doomed:
            self.storage.delete(plan.schema, record.record_id)
        self.storage.flush()
        for record in doomed:  # 行级事件在语句自身写入完成后派发
            self._fire_triggers(plan.schema, "DELETE", record.row, None)
        return ExecutionResult(affected_rows=len(doomed), message=f"已删除 {len(doomed)} 行")

    def _run_query(self, plan: QueryPlan) -> tuple[tuple[str, ...], list[Row]]:
        if isinstance(plan, EmptyScan):
            # 恒假条件：返回正确列名与零行，不访问存储。
            return plan.columns, []
        if isinstance(plan, Project):
            source_columns, source_rows = self._run_query(plan.source)
            indexes = {name: index for index, name in enumerate(source_columns)}
            # 先校验投影列，空表时也能发现未知列。
            for column in plan.columns:
                if column not in indexes:
                    raise _execution_error("UNKNOWN_COLUMN", column)
            projected: list[Row] = [
                tuple(row[indexes[column]] for column in plan.columns) for row in source_rows
            ]
            if plan.distinct:
                seen: set[Row] = set()
                deduped: list[Row] = []
                for row in projected:
                    if row not in seen:
                        seen.add(row)
                        deduped.append(row)
                projected = deduped
            if plan.order_by:
                # 从后往前对每个排序键做稳定排序，支持多列混合 ASC/DESC；
                # NULL 排序遵循固定契约：ASC 默认 NULL LAST、DESC 默认 NULL FIRST。
                column_index = {name: index for index, name in enumerate(plan.columns)}
                for name, descending in reversed(plan.order_by):
                    index = column_index[name]
                    valued = [(row[index], row) for row in projected]
                    nulls = [item for item in valued if item[0] is None]
                    non_nulls = [item for item in valued if item[0] is not None]
                    non_nulls.sort(key=lambda item: item[0], reverse=descending)
                    ordered = (nulls + non_nulls) if descending else (non_nulls + nulls)
                    projected = [row for _, row in ordered]
            if plan.limit is not None or plan.offset is not None:
                start = plan.offset or 0
                stop = start + plan.limit if plan.limit is not None else None
                projected = projected[start:stop]
            return plan.columns, projected
        columns = _query_columns(plan)
        return columns, [record.row for record in self._scan_records(plan)]

    def _scan_records(self, plan: SeqScan | Filter | EmptyScan) -> list[StoredRecord]:
        if isinstance(plan, SeqScan):
            return list(self.storage.scan(plan.schema))
        if isinstance(plan, EmptyScan):
            return []
        if isinstance(plan, Filter):
            indexes = {name: index for index, name in enumerate(_query_columns(plan.source))}
            kept: list[StoredRecord] = []
            for record in self._scan_records(plan.source):
                if self._eval_bool(plan.predicate, record.row, indexes):
                    kept.append(record)
            return kept
        raise _execution_error("UNKNOWN_PLAN", f"扫描源必须为 SeqScan/Filter: {type(plan).__name__}")

    def _eval_bool(self, expression: Expression, row: Row, indexes: dict[str, int]) -> bool | None:
        """WHERE 谓词求值：返回 True/False 或 None（UNKNOWN，按不选中处理）。"""
        value = self._eval(expression, row, indexes)
        if value is not None and not isinstance(value, bool):
            raise _execution_error("TYPE_MISMATCH", "WHERE 必须得到 BOOL")
        return value

    def _eval(self, expression: Expression, row: Row, indexes: dict[str, int]) -> Value:
        if isinstance(expression, Literal):
            return expression.value
        if isinstance(expression, Identifier):
            if expression.name not in indexes:
                raise _execution_error("UNKNOWN_COLUMN", expression.name)
            return row[indexes[expression.name]]
        if isinstance(expression, UnaryExpr):
            value = self._eval(expression.operand, row, indexes)
            if expression.operator == "NOT":
                if value is None:
                    return None  # NOT UNKNOWN = UNKNOWN
                if not isinstance(value, bool):
                    raise _execution_error("TYPE_MISMATCH", "NOT 需要 BOOL")
                return not value
            if expression.operator == "-":
                if value is None:
                    return None
                _require_int(value, "负号")
                return -value
            raise _execution_error("UNKNOWN_PLAN", f"未知一元运算符: {expression.operator}")
        if isinstance(expression, BinaryExpr):
            return self._eval_binary(expression, row, indexes)
        raise _execution_error("UNKNOWN_PLAN", f"未知表达式: {type(expression).__name__}")

    def _eval_binary(self, expression: BinaryExpr, row: Row, indexes: dict[str, int]) -> Value:
        operator = expression.operator
        left = self._eval(expression.left, row, indexes)
        right = self._eval(expression.right, row, indexes)
        if operator in ("AND", "OR"):
            # 三值逻辑：FALSE AND UNKNOWN = FALSE；TRUE OR UNKNOWN = TRUE。
            return boolean_values(operator, left, right)
        if operator in ("=", "!=", "<>", "<", "<=", ">", ">="):
            if left is None or right is None:
                return None  # 比较含 NULL 得 UNKNOWN
            # 与语义分析和常量折叠一致：同类型比较，严格区分 BOOL/INT。
            if type(left) is not type(right):
                raise _execution_error("TYPE_MISMATCH", "比较需要同类型操作数")
            if operator in ("<", "<=", ">", ">="):
                return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[operator]
            equal = left == right
            return equal if operator == "=" else not equal
        if operator in ("+", "-"):
            if left is None or right is None:
                return None
            _require_int(left, f"{operator} 左侧")
            _require_int(right, f"{operator} 右侧")
            result = left + right if operator == "+" else left - right
            if not (INT_MIN <= result <= INT_MAX):
                raise _execution_error("INTEGER_OUT_OF_RANGE", f"{operator} 运算结果超出 64 位有符号整数范围")
            return result
        raise _execution_error("UNKNOWN_PLAN", f"未知二元运算符: {operator}")
