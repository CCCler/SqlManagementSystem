from dataclasses import replace
import operator
from minisql.contracts.ast import BinaryExpr, Literal, UnaryExpr
from minisql.contracts.models import DataType
from minisql.contracts.plans import Update, Delete, EmptyScan, Explain, Filter, Plan, Project, QueryPlan, SeqScan


OPERATIONS = {
    "+": operator.add, "-": operator.sub, "=": operator.eq,
    "!=": operator.ne, "<>": operator.ne, "<": operator.lt,
    "<=": operator.le, ">": operator.gt, ">=": operator.ge,
    "AND": operator.and_, "OR": operator.or_,
}


def _plan_columns(plan: QueryPlan) -> tuple[str, ...]:
    """查询计划节点的输出列名；用于恒假条件构造空结果计划。"""
    if isinstance(plan, SeqScan):
        return tuple(column.name for column in plan.schema.columns)
    if isinstance(plan, Filter):
        return _plan_columns(plan.source)
    if isinstance(plan, (Project, EmptyScan)):
        return plan.columns
    raise TypeError(f"未知查询计划: {type(plan).__name__}")


class Optimizer:
    def optimize(self, plan: Plan) -> Plan:
        if isinstance(plan, Explain):
            return replace(plan, plan=self.optimize(plan.plan))
        if isinstance(plan, Filter):
            predicate = _fold(plan.predicate)
            if _boolean(predicate, True):
                # 恒真：消除 Filter，直接返回优化后的源计划。
                return self.optimize(plan.source)
            if _boolean(predicate, False):
                # 恒假：生成空结果计划，执行时不再扫描用户表。
                return EmptyScan(_plan_columns(plan.source))
            return replace(plan, predicate=predicate, source=self.optimize(plan.source))
        if isinstance(plan, Update):
            return replace(plan, assignments=tuple((name, _fold(value)) for name, value in plan.assignments),
                           source=self.optimize(plan.source))
        if isinstance(plan, (Project, Delete)):
            return replace(plan, source=self.optimize(plan.source))
        return replace(plan)


def _boolean(node, value):
    return isinstance(node, Literal) and node.data_type is DataType.BOOL and node.value is value


def _may_overflow(node):
    """仅对已通过语义检查的表达式使用；未折叠的整数运算可能报错。

    执行器按左右顺序求值（不短路），不能因吸收律丢弃可能溢出的子树。
    """
    if isinstance(node, BinaryExpr):
        return node.operator in ("+", "-") or _may_overflow(node.left) or _may_overflow(node.right)
    if isinstance(node, UnaryExpr):
        return node.operator == "-" or _may_overflow(node.operand)
    return False


def _fold(node):
    if isinstance(node, UnaryExpr):
        operand = _fold(node.operand)
        if isinstance(operand, Literal):
            return Literal(not operand.value, DataType.BOOL, node.position)
        if isinstance(operand, UnaryExpr) and operand.operator == "NOT":
            return operand.operand
        return replace(node, operand=operand)
    if not isinstance(node, BinaryExpr):
        return node
    left, right = _fold(node.left), _fold(node.right)
    if isinstance(left, Literal) and isinstance(right, Literal):
        value = OPERATIONS[node.operator](left.value, right.value)
        kind = DataType.INT if node.operator in ("+", "-") else DataType.BOOL
        if kind is not DataType.INT or -(2 ** 63) <= value < 2 ** 63:
            return Literal(value, kind, node.position)
    if node.operator == "AND":
        if _boolean(left, True):
            return right
        if _boolean(right, True):
            return left
        if ((_boolean(left, False) and not _may_overflow(right)) or
                (_boolean(right, False) and not _may_overflow(left))):
            return Literal(False, DataType.BOOL, node.position)
    if node.operator == "OR":
        if _boolean(left, False):
            return right
        if _boolean(right, False):
            return left
        if ((_boolean(left, True) and not _may_overflow(right)) or
                (_boolean(right, True) and not _may_overflow(left))):
            return Literal(True, DataType.BOOL, node.position)
    return replace(node, left=left, right=right)
