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
from minisql.contracts.models import SourcePosition

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
    """求值上下文：当前行 + 外层行链（相关子查询逐层回溯）+ 投影键映射（聚合后求值）。

    mapped：表达式结构键 → 当前行投影值，用于聚合/投影后的表达式按 expr_key
    取用已算好的值（如 HAVING COUNT(*) > 0 中的 COUNT(*)）。
    runner：子查询执行器（计划, 外层上下文）→ 值元组列表；由执行器注入。
    """

    __slots__ = ("row", "outer", "mapped", "runner")

    def __init__(self, row, outer=None, mapped=None, runner=None):
        self.row = row
        self.outer = outer
        self.mapped = mapped
        self.runner = runner

    def lookup(self, scope, source):
        context = self
        while context is not None:
            row = context.row
            if row is not None and (scope, source) in row.values:
                return row.values[(scope, source)]
            context = context.outer
        return None

    def derive(self, row, mapped=None):
        """派生一个子上下文：沿用外层链并继承子查询执行器。"""
        return RowContext(row, self, mapped, self.runner)


def expr_key(expr):
    """与编译器一致的结构键：绑定字段按 (scope, source, ordinal)，其余按结构。"""
    if getattr(expr, "binding", None):
        b = expr.binding
        return ("field", b.scope, b.source, b.ordinal)
    from minisql.contracts.extensions import Expr
    return (expr.op, tuple(expr_key(a) if isinstance(a, Expr) else repr(a) for a in expr.args))


def evaluate(expr, context: RowContext):
    """求一个扩展表达式的值；返回 None / bool / int / Decimal / str / 日期时间。"""
    if context.mapped:
        key = expr_key(expr)
        if key in context.mapped:
            return context.mapped[key]
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
        from minisql.contracts.extensions import Expr
        if any(not isinstance(arg, Expr) for arg in expr.args[1:]):
            # 子查询形式：x IN (SELECT ...)，按三值 OR 逐值比较
            left = evaluate(expr.args[0], context)
            plan = expr.args[1]
            result = False
            for values in (context.runner(plan, context) if context.runner else ()):
                result = boolean_values("OR", result, _compare("=", left, values[0]))
                if result is True:
                    return True
            return result
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
    if op == "scalar":
        values = _run_subquery(expr, context)
        if not values:
            return None
        if len(values) > 1:
            raise _error("SUBQUERY_MULTIPLE_ROWS", "标量子查询最多返回一行")
        return values[0][0]
    if op == "EXISTS":
        return len(_run_subquery(expr, context)) > 0
    if op in ("COUNT", "SUM", "AVG", "MAX", "MIN"):
        raise _error("FEATURE_NOT_EXECUTABLE", f"聚合 {op} 需由 Aggregate 算子求值")
    if op == "star":
        raise _error("FEATURE_NOT_EXECUTABLE", "表达式 * 的执行尚未接入")
    raise _error("FEATURE_NOT_EXECUTABLE", f"表达式 {op} 的执行尚未接入")


def _run_subquery(expr, context):
    """执行标量/EXISTS 子查询计划，返回值元组列表。"""
    if context.runner is None:
        raise _error("FEATURE_NOT_EXECUTABLE", "子查询执行器未接入")
    return context.runner(expr.args[0], context)


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


def serialize_expr(expr) -> str:
    """把约束/默认值表达式序列化为 JSON 文本（仅结构 + 列序号 + 字面量）。

    字段引用只保留 ordinal（即表内列序号），检查时重建为 (0, 0) 来源的绑定；
    DECIMAL/日期时间字面量带类型标记，反序列化后还原为精确值。"""
    import json

    def encode(node):
        if node.op == "literal":
            return {"op": "literal", "value": _encode_value(node.args[0])}
        if node.op == "column":
            binding = node.binding
            return {"op": "column", "ordinal": binding.ordinal if binding else None,
                    "name": binding.name if binding else node.args[-1]}
        from minisql.contracts.extensions import Expr
        return {"op": node.op,
                "args": [encode(arg) if isinstance(arg, Expr) else _encode_value(arg)
                         for arg in node.args]}

    return json.dumps(encode(expr), ensure_ascii=False)


def _encode_value(value):
    if isinstance(value, Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if isinstance(value, time):
        return {"__time__": value.isoformat()}
    return value


def _decode_value(value):
    if isinstance(value, dict):
        if "__decimal__" in value:
            return Decimal(value["__decimal__"])
        if "__datetime__" in value:
            return datetime.fromisoformat(value["__datetime__"])
        if "__date__" in value:
            return date.fromisoformat(value["__date__"])
        if "__time__" in value:
            return time.fromisoformat(value["__time__"])
    return value


def _value_spec(value):
    """字面量的编译期类型标注（编译器读取默认值/约束时要求 type 非空）。"""
    from minisql.contracts.extensions import TypeSpec
    if isinstance(value, bool):
        return TypeSpec("BOOL", nullable=False)
    if isinstance(value, int):
        return TypeSpec("INT", nullable=False)
    if isinstance(value, str):
        return TypeSpec("VARCHAR", nullable=False)
    if isinstance(value, Decimal):
        sign, digits, exponent = value.as_tuple()
        scale = max(0, -exponent)
        return TypeSpec("DECIMAL", max(1, len(digits)), min(scale, 38), False)
    if isinstance(value, datetime):
        return TypeSpec("TIMESTAMP", nullable=False)
    if isinstance(value, date):
        return TypeSpec("DATE", nullable=False)
    if isinstance(value, time):
        return TypeSpec("TIME", nullable=False)
    return TypeSpec("NULL")


def deserialize_expr(text: str, columns: tuple = (), types: tuple = ()):
    """还原 serialize_expr 的结果；columns 为列名，types 为对应的 TypeSpec（可省略）。"""
    import json
    from minisql.contracts.extensions import Expr, FieldBinding, TypeSpec

    def decode(node):
        op = node["op"]
        if op == "literal":
            value = _decode_value(node["value"])
            return Expr("literal", (value,), SourcePosition(1, 1), _value_spec(value))
        if op == "column":
            ordinal = node["ordinal"]
            if ordinal is None or ordinal >= len(columns) or columns[ordinal] != node["name"]:
                raise _error("INVALID_CONSTRAINT", f"约束引用了未知列：{node['name']}")
            spec = types[ordinal] if ordinal < len(types) else TypeSpec("NULL")
            binding = FieldBinding(0, 0, ordinal, "", node["name"], spec)
            return Expr("column", (None, node["name"]), SourcePosition(1, 1), spec, binding)
        args = tuple(decode(arg) if isinstance(arg, dict) and "op" in arg else _decode_value(arg)
                     for arg in node["args"])
        return Expr(op, args, SourcePosition(1, 1))

    try:
        return decode(json.loads(text))
    except MiniSQLError:
        raise
    except Exception as error:
        raise _error("INVALID_CONSTRAINT", f"约束表达式无法还原：{error}") from error


def _sub_context(row, outer):
    """聚合成员的求值上下文：无外层时新建，有外层时沿用链并继承 runner。"""
    return outer.derive(row) if outer is not None else RowContext(row)


def aggregate_value(expression, rows, outer):
    """聚合函数求值：COUNT(*) 计全部行、COUNT(expr) 跳过 NULL；
    SUM/AVG/MAX/MIN 忽略 NULL，空输入为 NULL；SUM 溢出与 DECIMAL 位数按契约报错。"""
    op = expression.op
    if op == "COUNT":
        if not expression.args:
            return len(rows)
        return sum(1 for row in rows
                   if evaluate(expression.args[0], _sub_context(row, outer)) is not None)
    values = [value for value in
              (evaluate(expression.args[0], _sub_context(row, outer)) for row in rows)
              if value is not None]
    if not values:
        return None
    if op == "SUM":
        if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
            total = sum(values)
            if not INT_MIN <= total <= INT_MAX:
                raise _error("INTEGER_OUT_OF_RANGE", "SUM 结果超出 64 位有符号整数范围")
            return total
        _require_numeric(values[0], "SUM ")
        with localcontext() as ctx:
            ctx.prec = _WORKING_PRECISION
            total = sum(Decimal(value) for value in values)
        return _fit_decimal("+", total, expression.type)
    if op == "AVG":
        _require_numeric(values[0], "AVG ")
        with localcontext() as ctx:
            ctx.prec = _WORKING_PRECISION
            average = sum(Decimal(value) for value in values) / len(values)
        return _fit_decimal("/", average, expression.type)
    kinds = {_kind(value) for value in values}
    if len(kinds) > 1 and not kinds <= {"INT", "DECIMAL"}:
        raise _error("TYPE_MISMATCH", f"{op} 要求同类型值")
    return max(values) if op == "MAX" else min(values)


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
