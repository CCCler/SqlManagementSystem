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
        return "编译计划（执行待接入：" + ", ".join(collect_capabilities(plan)) + "）\n" + json.dumps(to_json_value(plan), ensure_ascii=False, indent=2)
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


class PlanExecutor:
    def __init__(self, storage: RecordStorage, catalog: CatalogWriter) -> None:
        self.storage = storage
        self.catalog = catalog

    def execute(self, plan: Plan) -> ExecutionResult:
        from minisql.contracts.extensions import ExtendedPlan
        if isinstance(plan, ExtendedPlan) and plan.operator == "Explain":
            return ExecutionResult(message=render_plan(plan.children[0]))
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

    def _create_table(self, plan: CreateTable) -> ExecutionResult:
        # 先分配物理结构，成功后登记 Catalog；登记失败不写目录。
        assigned = self.storage.create_table(plan.schema)
        self.catalog.register_table(assigned)
        self.storage.flush()
        return ExecutionResult(message=f"表 {assigned.name} 已创建")

    def _insert(self, plan: Insert) -> ExecutionResult:
        self.storage.insert(plan.schema, plan.values)
        self.storage.flush()
        return ExecutionResult(affected_rows=1, message="已插入 1 行")

    def _drop_table(self, plan: DropTable) -> ExecutionResult:
        if plan.schema.table_id == 0 or plan.schema.name.lower() == "__catalog":
            raise _execution_error("PROTECTED_TABLE", "不能删除系统目录表")
        # 防止旧计划误删同名重建后的新表。
        current = self.catalog.get_table(plan.schema.name)
        if current is None or current.table_id != plan.schema.table_id:
            raise _execution_error("UNKNOWN_TABLE", plan.schema.name)
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
        for record_id, row in replacements:
            # 复用堆存储支持变长记录迁移；事务日志负责整条语句的失败恢复。
            # 先插入再删除，不改变尚未更新记录的槽编号。
            self.storage.insert(plan.schema, row)
            self.storage.delete(plan.schema, record_id)
        self.storage.flush()
        count = len(replacements)
        return ExecutionResult(affected_rows=count, message=f"已更新 {count} 行")

    def _delete(self, plan: Delete) -> ExecutionResult:
        if not isinstance(plan.source, (SeqScan, Filter, EmptyScan)):
            raise _execution_error("UNKNOWN_PLAN", "DELETE 只允许 SeqScan/Filter/EmptyScan 源以保留 RecordId")
        count = 0
        for record in self._scan_records(plan.source):
            self.storage.delete(plan.schema, record.record_id)
            count += 1
        self.storage.flush()
        return ExecutionResult(affected_rows=count, message=f"已删除 {count} 行")

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
                # 从后往前对每个排序键做稳定排序，支持多列混合 ASC/DESC。
                column_index = {name: index for index, name in enumerate(plan.columns)}
                for name, descending in reversed(plan.order_by):
                    index = column_index[name]
                    projected.sort(key=lambda row, i=index: row[i], reverse=descending)
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

    def _eval_bool(self, expression: Expression, row: Row, indexes: dict[str, int]) -> bool:
        value = self._eval(expression, row, indexes)
        if not isinstance(value, bool):
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
                if not isinstance(value, bool):
                    raise _execution_error("TYPE_MISMATCH", "NOT 需要 BOOL")
                return not value
            if expression.operator == "-":
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
            if not isinstance(left, bool) or not isinstance(right, bool):
                raise _execution_error("TYPE_MISMATCH", f"{operator} 需要 BOOL")
            return left and right if operator == "AND" else left or right
        if operator in ("=", "!=", "<>"):
            if type(left) is not type(right):
                raise _execution_error("TYPE_MISMATCH", "比较需要同类型操作数")
            equal = left == right
            return equal if operator == "=" else not equal
        if operator in ("<", "<=", ">", ">="):
            # 与语义分析和常量折叠一致：同类型比较，严格区分 BOOL/INT。
            if type(left) is not type(right):
                raise _execution_error("TYPE_MISMATCH", "比较需要同类型操作数")
            return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[operator]
        if operator in ("+", "-"):
            _require_int(left, f"{operator} 左侧")
            _require_int(right, f"{operator} 右侧")
            result = left + right if operator == "+" else left - right
            if not (INT_MIN <= result <= INT_MAX):
                raise _execution_error("INTEGER_OUT_OF_RANGE", f"{operator} 运算结果超出 64 位有符号整数范围")
            return result
        raise _execution_error("UNKNOWN_PLAN", f"未知二元运算符: {operator}")
