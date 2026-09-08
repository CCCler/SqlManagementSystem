"""PlanExecutor：执行 CreateTable/Insert/SeqScan/Filter/Project/Delete 计划。

SeqScan/Filter 保留 StoredRecord 的 RecordId；SELECT 的 Project 产生结果列；
DELETE 只接收扫描或过滤结果，禁止 Project 以保留 RecordId。"""
from minisql.contracts.ast import BinaryExpr, Expression, Identifier, Literal, UnaryExpr
from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.interfaces import CatalogWriter, RecordStorage
from minisql.contracts.models import ExecutionResult, Row, StoredRecord, Value
from minisql.contracts.plans import (
    CreateTable, Delete, Filter, Insert, Plan, Project, QueryPlan, SeqScan,
)


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
    if isinstance(plan, Project):
        return plan.columns
    raise _execution_error("UNKNOWN_PLAN", f"未知查询计划: {type(plan).__name__}")


class PlanExecutor:
    def __init__(self, storage: RecordStorage, catalog: CatalogWriter) -> None:
        self.storage = storage
        self.catalog = catalog

    def execute(self, plan: Plan) -> ExecutionResult:
        if isinstance(plan, CreateTable):
            return self._create_table(plan)
        if isinstance(plan, Insert):
            return self._insert(plan)
        if isinstance(plan, Delete):
            return self._delete(plan)
        if isinstance(plan, (SeqScan, Filter, Project)):
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

    def _delete(self, plan: Delete) -> ExecutionResult:
        if not isinstance(plan.source, (SeqScan, Filter)):
            raise _execution_error("UNKNOWN_PLAN", "DELETE 只允许 SeqScan/Filter 源以保留 RecordId")
        count = 0
        for record in self._scan_records(plan.source):
            self.storage.delete(plan.schema, record.record_id)
            count += 1
        self.storage.flush()
        return ExecutionResult(affected_rows=count, message=f"已删除 {count} 行")

    def _run_query(self, plan: QueryPlan) -> tuple[tuple[str, ...], list[Row]]:
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
            return plan.columns, projected
        columns = _query_columns(plan)
        return columns, [record.row for record in self._scan_records(plan)]

    def _scan_records(self, plan: SeqScan | Filter) -> list[StoredRecord]:
        if isinstance(plan, SeqScan):
            return list(self.storage.scan(plan.schema))
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
            _require_int(left, f"{operator} 左侧")
            _require_int(right, f"{operator} 右侧")
            return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[operator]
        if operator in ("+", "-"):
            _require_int(left, f"{operator} 左侧")
            _require_int(right, f"{operator} 右侧")
            return left + right if operator == "+" else left - right
        raise _execution_error("UNKNOWN_PLAN", f"未知二元运算符: {operator}")
