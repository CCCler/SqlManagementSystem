"""保守扩展优化：仅折叠安全常量，不移除潜在运行期错误。"""
from dataclasses import replace
from decimal import Decimal, localcontext, ROUND_HALF_EVEN, DecimalException
import operator
from minisql.contracts.extensions import Expr, ExtendedPlan, ObjectDependency


def fold_expr(expr):
    args = tuple(fold_expr(a) if isinstance(a, Expr) else a for a in expr.args)
    result = replace(expr, args=args)
    if expr.op in ('literal', 'column') or not args or not all(isinstance(a, Expr) and a.op == 'literal' for a in args):
        return result
    values = [a.args[0] for a in args]
    op = expr.op
    try:
        if op in ('IS NULL', 'IS NOT NULL'):
            value = (values[0] is None) == (op == 'IS NULL')
        elif op == 'NOT':
            value = None if values[0] is None else not values[0]
        elif op in ('AND', 'OR'):
            a, b = values
            if op == 'AND':
                value = False if a is False or b is False else None if a is None or b is None else True
            else:
                value = True if a is True or b is True else None if a is None or b is None else False
        elif op in ('+', '-', '*', '/', '=', '!=', '<>', '<', '<=', '>', '>='):
            if any(v is None for v in values):
                value = None
            else:
                ops = {'+': operator.add, '-': operator.sub, '*': operator.mul, '/': operator.truediv,
                       '=': operator.eq, '!=': operator.ne, '<>': operator.ne, '<': operator.lt,
                       '<=': operator.le, '>': operator.gt, '>=': operator.ge}
                with localcontext() as ctx:
                    ctx.prec = 100
                    a, b = values
                    if expr.type.kind == 'DECIMAL':
                        a, b = Decimal(a), Decimal(b)
                    value = ops[op](a, b)
                    if expr.type.kind == 'INT' and not -(2**63) <= value < 2**63:
                        return result
                    if expr.type.kind == 'DECIMAL':
                        quantum = Decimal(1).scaleb(-expr.type.scale)
                        rounded = value.quantize(quantum, rounding=ROUND_HALF_EVEN)
                        if op != '/' and rounded != value:
                            return result
                        if abs(rounded) >= Decimal(10)**(expr.type.precision-expr.type.scale):
                            return result
                        value = rounded
        else:
            return result
    except (ArithmeticError, DecimalException, ValueError, TypeError):
        return result
    return replace(expr, op='literal', args=(value,))


def optimize(plan, catalog):
    children = tuple(optimize(c, catalog) if isinstance(c, ExtendedPlan) else c for c in plan.children)
    expressions = tuple(fold_expr(e) for e in plan.expressions)
    dependencies = set(plan.dependencies)
    for child in children:
        if isinstance(child, ExtendedPlan):
            dependencies.update(child.dependencies)
    result = replace(plan, children=children, expressions=expressions,
                     dependencies=tuple(sorted(dependencies, key=lambda d: (d.kind, d.name))))
    if plan.operator != 'Filter' or not children or children[0].operator != 'TableScan':
        return result
    reader = getattr(catalog, 'list_indexes', None)
    if reader is None:
        return result
    scan = children[0]
    table = dict(scan.attributes)['table']
    def terms(expr):
        if expr.op == 'AND':
            return terms(expr.args[0])+terms(expr.args[1])
        return [expr]
    candidates = []
    predicates = terms(expressions[0])
    def safe(expr):
        if expr.op not in ('literal', 'column', '=', '!=', '<>', '<', '<=', '>', '>=', 'AND', 'OR', 'NOT', 'IS NULL', 'IS NOT NULL'):
            return False
        return all(safe(a) for a in expr.args if isinstance(a, Expr))
    if not safe(expressions[0]):
        return result  # 缩窄扫描不能掩盖非匹配行上原本会发生的算术/子查询错误。
    for index in reader(table):
        if not index.available or index.table != table:
            continue
        equal = 0
        bounds = []
        has_range = False
        for column in index.columns:
            matches = []
            for predicate in predicates:
                if predicate.op not in ('=', '<', '<=', '>', '>='):
                    continue
                left, right = predicate.args
                op = predicate.op
                if left.op == 'literal' and right.op == 'column':
                    left, right = right, left
                    op = {'<': '>', '<=': '>=', '>': '<', '>=': '<=', '=': '='}[op]
                if left.op == 'column' and left.binding and left.binding.name == column and right.op == 'literal' and right.args[0] is not None:
                    matches.append((column, op, right))
            eq = [m for m in matches if m[1] == '=']
            if eq:
                equal += 1
                bounds.extend(eq)
            elif matches:
                bounds.extend(matches)
                has_range = True
                break
            else:
                break
        if bounds:
            candidates.append((-equal, -int(has_range), index.name, index, tuple(bounds)))
    if not candidates:
        return result
    _, _, _, index, bounds = min(candidates, key=lambda c: c[:3])
    capabilities = tuple(sorted(set(scan.capabilities) | {'index_scan'}))
    selected = replace(scan, operator='IndexScan', attributes=scan.attributes+(('index', index.name), ('bounds', bounds)), capabilities=capabilities, dependencies=scan.dependencies+(ObjectDependency('index', index.name),))
    # 保留全部谓词做残余检查，包含范围外的列及潜在错误。
    return replace(result, children=(selected,), dependencies=result.dependencies+(ObjectDependency('index', index.name),), capabilities=tuple(sorted(set(result.capabilities) | {'index_scan'})))


def collect_capabilities(plan):
    result = set(plan.capabilities)
    def visit(value):
        if isinstance(value, ExtendedPlan):
            result.update(collect_capabilities(value))
        elif isinstance(value, Expr):
            for arg in value.args:
                visit(arg)
        elif isinstance(value, (tuple, list)):
            for arg in value:
                visit(arg)
    visit(plan.children)
    visit(plan.expressions)
    return tuple(sorted(result))
