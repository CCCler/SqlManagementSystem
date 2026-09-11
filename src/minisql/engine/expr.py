"""扩展表达式求值与运行期行上下文（F01—F08 执行侧）。

语义遵循 docs/SQL扩展编译器报告.md 第 3 节固定契约：
- 比较遇 NULL 得 UNKNOWN；AND/OR/NOT 三值逻辑；IS NULL 恒为 BOOL。
- LIKE 区分大小写，支持 %、_ 与单字符 ESCAPE；BETWEEN 含两端。
- INT 保持有符号 64 位，运行期溢出报 INTEGER_OUT_OF_RANGE；除零报错。
- DECIMAL 为精确值：结果位数不足报 NUMERIC_OUT_OF_RANGE 不静默截断，
  除法按 ROUND_HALF_EVEN 舍入；布尔与 INT 严格区分。
聚合（COUNT/SUM/AVG/MAX/MIN）与子查询表达式由对应算子求值，本模块拒绝直接求值。
"""
import re
from datetime import date, datetime, time
from decimal import Decimal, ROUND_HALF_EVEN, localcontext

from minisql.contracts.errors import ErrorStage, MiniSQLError

INT_MIN, INT_MAX = -(2 ** 63), 2 ** 63 - 1
_WORKING_PRECISION = 60


def _error(code: str, reason: str) -> MiniSQLError:
    return MiniSQLError(ErrorStage.EXECUTION, code, reason)


class Row:
    """运行期行：绑定键 (scope, source) → 列值元组，外加当前投影输出。"""

    __slots__ = ("values", "output")

    def __init__(self, values=None, output=None):
        self.values = values if values is not None else {}
        self.output = output


class RowContext:
    """求值上下文：当前行 + 外层行链（相关子查询逐层回溯）。"""

    __slots__ = ("row", "outer")

    def __init__(self, row, outer=None):
        self.row = row
        self.outer = outer

    def lookup(self, scope, source):
        context = self
        while context is not None:
            row = context.row
            if row is not None and (scope, source) in row.values:
                return row.values[(scope, source)]
            context = context.outer
        return None


def expr_key(expr):
    """与编译器一致的结构键：绑定字段按 (scope, source, ordinal)，其余按结构。"""
    if getattr(expr, "binding", None):
        b = expr.binding
        return ("field", b.scope, b.source, b.ordinal)
    from minisql.contracts.extensions import Expr
    return (expr.op, tuple(expr_key(a) if isinstance(a, Expr) else repr(a) for a in expr.args))


def evaluate(expr, context: RowContext):
    """求一个扩展表达式的值；返回 None / bool / int / Decimal / str / 日期时间。"""
    op = expr.op
    if op == "literal":
        return expr.args[0]
    if op == "column":
        binding = expr.binding
        row = context.lookup(binding.scope, binding.source)
        if row is None:
            raise _error("UNKNOWN_COLUMN", f"运行期缺少字段绑定：{binding.name}")
        return row[binding.ordinal]
    if op in ("+", "-", "*", "/"):
        return _arithmetic(op, expr, context)
    if op in ("AND", "OR", "NOT"):
        return _boolean(op, expr, context)
    if op in ("=", "!=", "<>", "<", "<=", ">", ">="):
        return _compare(op, evaluate(expr.args[0], context), evaluate(expr.args[1], context))
    if op == "IS NULL":
        return evaluate(expr.args[0], context) is None
    if op == "IS NOT NULL":
        return evaluate(expr.args[0], context) is not None
    if op == "BETWEEN":
        value = evaluate(expr.args[0], context)
        low = evaluate(expr.args[1], context)
        high = evaluate(expr.args[2], context)
        return boolean_values("AND", _compare(">=", value, low), _compare("<=", value, high))
    if op == "IN":
        value = evaluate(expr.args[0], context)
        result = False
        for item in expr.args[1:]:
            result = boolean_values("OR", result, _compare("=", value, evaluate(item, context)))
            if result is True:
                return True
        return result
    if op == "LIKE":
        args = [evaluate(a, context) for a in expr.args]
        return _like(args)
    if op in ("COUNT", "SUM", "AVG", "MAX", "MIN"):
        raise _error("FEATURE_NOT_EXECUTABLE", f"聚合 {op} 需由 Aggregate 算子求值")
    if op in ("scalar", "EXISTS") or op == "star":
        raise _error("FEATURE_NOT_EXECUTABLE", f"表达式 {op} 的执行尚未接入")
    raise _error("FEATURE_NOT_EXECUTABLE", f"表达式 {op} 的执行尚未接入")


def _require_numeric(value, context=""):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise _error("TYPE_MISMATCH", f"{context}需要 INT/DECIMAL")


def _require_bool(value, context=""):
    if value is not None and not isinstance(value, bool):
        raise _error("TYPE_MISMATCH", f"{context}需要 BOOL")


def _arithmetic(op, expr, context):
    left = evaluate(expr.args[0], context)
    right = evaluate(expr.args[1], context)
    if left is None or right is None:
        return None
    _require_numeric(left)
    _require_numeric(right)
    if op == "/" and right == 0:
        raise _error("DIVISION_BY_ZERO", "除数为零")
    if isinstance(left, int) and isinstance(right, int) and op != "/":
        value = {"+": left + right, "-": left - right, "*": left * right}[op]
        if not INT_MIN <= value <= INT_MAX:
            raise _error("INTEGER_OUT_OF_RANGE", f"{op} 运算结果超出 64 位有符号整数范围")
        return value
    with localcontext() as ctx:
        ctx.prec = _WORKING_PRECISION
        left_dec, right_dec = Decimal(left), Decimal(right)
        if op == "+":
            value = left_dec + right_dec
        elif op == "-":
            value = left_dec - right_dec
        elif op == "*":
            value = left_dec * right_dec
        else:
            value = left_dec / right_dec
    return _fit_decimal(op, value, expr.type)


def _fit_decimal(op, value: Decimal, spec):
    """把精确结果装进声明的 DECIMAL(p, s)：位数不足报错，除法按 HALF_EVEN 舍入。"""
    if spec is None or spec.kind != "DECIMAL":
        return value
    precision = spec.precision if spec.precision is not None else 18
    scale = spec.scale if spec.scale is not None else 2
    if not value.is_finite():
        raise _error("NUMERIC_OUT_OF_RANGE", "DECIMAL 结果超出可表示范围")
    sign, digits, exponent = value.as_tuple()
    integral_digits = max(0, len(digits) + exponent)
    if integral_digits > precision - scale:
        raise _error("NUMERIC_OUT_OF_RANGE",
                     f"结果整数位 {integral_digits} 超出 DECIMAL({precision},{scale}) 容量")
    fraction_digits = max(0, -exponent)
    quantum = Decimal(1).scaleb(-scale)
    if fraction_digits <= scale:
        return value.quantize(quantum)
    if op != "/":
        raise _error("NUMERIC_OUT_OF_RANGE",
                     f"结果小数位 {fraction_digits} 超出 DECIMAL({precision},{scale})，不能丢弃非零小数")
    with localcontext() as ctx:
        ctx.prec = _WORKING_PRECISION
        rounded = value.quantize(quantum, rounding=ROUND_HALF_EVEN)
    digits = rounded.as_tuple().digits
    if max(0, len(digits) + rounded.as_tuple().exponent) > precision - scale:
        raise _error("NUMERIC_OUT_OF_RANGE", "舍入后结果超出 DECIMAL 容量")
    return rounded


def _boolean(op, expr, context):
    if op == "NOT":
        value = evaluate(expr.args[0], context)
        _require_bool(value, "NOT ")
        return None if value is None else not value
    left = evaluate(expr.args[0], context)
    right = evaluate(expr.args[1], context)
    return boolean_values(op, left, right)


def boolean_values(op, left, right):
    _require_bool(left, f"{op} ")
    _require_bool(right, f"{op} ")
    if op == "AND":
        if left is False or right is False:
            return False
        if left is None or right is None:
            return None
        return True
    if left is True or right is True:
        return True
    if left is None or right is None:
        return None
    return False


def _compare(op, left, right):
    if left is None or right is None:
        return None
    left_kind, right_kind = _kind(left), _kind(right)
    numeric = {"BOOL", "INT", "DECIMAL"}
    if left_kind in ("INT", "DECIMAL") and right_kind in ("INT", "DECIMAL"):
        pass  # 数值之间可比较
    elif left_kind != right_kind:
        raise _error("TYPE_MISMATCH", f"比较要求同类型操作数：{left_kind}/{right_kind}")
    outcomes = {
        "=": left == right, "!=": left != right, "<>": left != right,
        "<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right,
    }
    return outcomes[op]


def _kind(value):
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT"
    if isinstance(value, Decimal):
        return "DECIMAL"
    if isinstance(value, str):
        return "VARCHAR"
    if isinstance(value, datetime):
        return "TIMESTAMP"
    if isinstance(value, date):
        return "DATE"
    if isinstance(value, time):
        return "TIME"
    return type(value).__name__.upper()


def _like(args):
    value, pattern = args[0], args[1]
    if value is None or pattern is None:
        return None
    escape = args[2] if len(args) == 3 else None
    return re.fullmatch(_like_regex(pattern, escape), value, re.DOTALL) is not None


def _like_regex(pattern: str, escape) -> str:
    parts = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if escape is not None and char == escape:
            index += 1
            if index >= len(pattern):
                raise _error("INVALID_ESCAPE", "LIKE 转义符后缺少字符")
            parts.append(re.escape(pattern[index]))
        elif char == "%":
            parts.append(".*")
        elif char == "_":
            parts.append(".")
        else:
            parts.append(re.escape(char))
        index += 1
    return "".join(parts)
