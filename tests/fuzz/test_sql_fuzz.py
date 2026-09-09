"""完整 SQL Fuzz 测试。

固定随机种子按支持文法与 Catalog 状态生成合法 SQL，并对合法语句做定向变异，
覆盖缺符号、括号、错误类型、未知表列、字符串、注释和复杂度边界。

检查四类问题：
- 误接受：可证明非法的变异却编译成功；
- 误拒绝：生成的合法 SQL 编译或执行失败；
- 未处理异常：MiniSQLError 之外的异常；
- 优化等价性：plan 与 optimized_plan 在同一状态上执行结果与库状态一致。
词法/语法/语义错误必须携带位置。

任一失败都在断言消息中给出种子、案例序号与 SQL；直接用 SEED 重跑本文件即可
复现。运行时间通过固定案例数控制，不使用时间阈值断言。
"""
import random
import re
from copy import deepcopy

import pytest

from minisql.compiler.compiler import SQLCompiler
from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType
from minisql.contracts.plans import Explain
from minisql.engine.executor import PlanExecutor
from tests.fakes.memory import MemoryCatalog, MemoryStorage

SEED = 20260909
VALID_STATEMENTS = 40
MUTATION_CASES = 120

VALID = "valid"
INVALID = "invalid"
EITHER = "either"

INT_VALUES = (-100, -1, 0, 1, 2, 17, 99)
STR_VALUES = ("x", "中文", "a;b", "it''s", "")

_INT_COMPARISON = re.compile(r"(\w+)\s*(=|!=|<>|<|<=|>|>=)\s*(-?\d+)")
_STR_COMPARISON = re.compile(r"(\w+)\s*(=|!=|<>|<|<=|>|>=)\s*('[^']*')")
_INT_LITERAL = re.compile(r"(?<![A-Za-z_])(-?\d+)\b")


class Model:
    """Fuzz 自身的 Catalog 状态模型：表名到列定义，用于生成合法引用。"""

    def __init__(self):
        self.tables: dict[str, tuple[ColumnSchema, ...]] = {}

    def add(self, name, columns):
        self.tables[name] = columns

    def remove(self, name):
        del self.tables[name]


def fresh_db():
    storage = MemoryStorage()
    catalog = MemoryCatalog()
    catalog.bootstrap()
    return storage, catalog


def db_state(storage, catalog):
    return (storage.tables, storage.records, storage.next_table, storage.next_slot, catalog.tables)


def compile_sql(sql, catalog):
    return SQLCompiler().compile(sql, catalog)


def execute_plan(plan, storage, catalog):
    return PlanExecutor(storage, catalog).execute(plan)


def check_equivalence(sql, storage, catalog, case_id):
    """编译成功后验证优化前后执行结果与库状态等价；编译失败向上抛 MiniSQLError。"""
    compiled = compile_sql(sql, catalog)
    other_storage, other_catalog = deepcopy(storage), deepcopy(catalog)
    first = execute_plan(compiled.plan, storage, catalog)
    second = execute_plan(compiled.optimized_plan, other_storage, other_catalog)
    if not isinstance(compiled.plan, Explain):
        assert first == second, f"优化前后结果不等 {case_id}: {sql!r}"
    assert db_state(storage, catalog) == db_state(other_storage, other_catalog), (
        f"执行后库状态不等 {case_id}: {sql!r}")


# ---------- 合法 SQL 生成 ----------

def gen_literal(rng, data_type):
    if data_type is DataType.INT:
        return str(rng.choice(INT_VALUES))
    return "'" + rng.choice(STR_VALUES) + "'"


def gen_where(rng, columns, depth=0):
    """生成类型正确的布尔表达式；深度受控避免生成过深结构。"""
    if depth >= 3 or rng.random() < 0.4:
        column = rng.choice(columns)
        if column.data_type is DataType.INT:
            return f"{column.name} {rng.choice(('=', '!=', '<', '<=', '>', '>='))} {rng.choice(INT_VALUES)}"
        return f"{column.name} {rng.choice(('=', '!='))} '{rng.choice(STR_VALUES)}'"
    left = gen_where(rng, columns, depth + 1)
    right = gen_where(rng, columns, depth + 1)
    expression = f"{left} {rng.choice(('AND', 'OR'))} {right}"
    if rng.random() < 0.5:
        expression = f"NOT ({expression})"
    if rng.random() < 0.3:
        expression = f"({expression})"
    return expression


def gen_create(rng, model):
    if len(model.tables) >= 3:
        return None
    name = next(name for name in ("t0", "t1", "t2") if name not in model.tables)
    pool = [
        ColumnSchema("id", DataType.INT), ColumnSchema("age", DataType.INT),
        ColumnSchema("score", DataType.INT), ColumnSchema("name", DataType.VARCHAR),
        ColumnSchema("note", DataType.VARCHAR),
    ]
    columns = rng.sample(pool, rng.randint(2, 3))
    model.add(name, tuple(columns))
    return f"CREATE TABLE {name}(" + ", ".join(f"{c.name} {c.data_type.value}" for c in columns) + ");"


def gen_insert(rng, model):
    if not model.tables:
        return None
    name, columns = rng.choice(tuple(model.tables.items()))
    order = list(columns)
    rng.shuffle(order)
    cols = ", ".join(c.name for c in order)
    values = ", ".join(gen_literal(rng, c.data_type) for c in order)
    return f"INSERT INTO {name}({cols}) VALUES ({values});"


def gen_select(rng, model):
    if not model.tables:
        return None
    name, columns = rng.choice(tuple(model.tables.items()))
    if rng.random() < 0.4:
        cols = "*"
    else:
        cols = ", ".join(c.name for c in rng.sample(list(columns), rng.randint(1, len(columns))))
    sql = "SELECT "
    if rng.random() < 0.2:
        sql += "DISTINCT "
    sql += f"{cols} FROM {name}"
    if rng.random() < 0.7:
        sql += " WHERE " + gen_where(rng, columns)
    if rng.random() < 0.2:
        sql += f" LIMIT {rng.choice((1, 2, 3, 10))}"
    return sql + ";"


def gen_delete(rng, model):
    if not model.tables:
        return None
    name, columns = rng.choice(tuple(model.tables.items()))
    sql = f"DELETE FROM {name}"
    if rng.random() < 0.7:
        sql += " WHERE " + gen_where(rng, columns)
    return sql + ";"


def gen_drop(rng, model):
    if not model.tables:
        return None
    name, _ = rng.choice(tuple(model.tables.items()))
    model.remove(name)
    return f"DROP TABLE {name};"


def gen_explain(rng, model):
    select = gen_select(rng, model)
    if select is None:
        return None
    return "EXPLAIN " + select[:-1] + ";"


def gen_statement(rng, model):
    generators = (gen_create, gen_insert, gen_select, gen_delete, gen_drop, gen_explain)
    for _ in range(20):
        sql = generators[rng.randrange(len(generators))](rng, model)
        if sql is not None:
            return sql
    return None


# ---------- 定向变异 ----------

def mutate_drop_semicolon(sql, model):
    return sql[:-1], INVALID


def mutate_unknown_table(sql, model):
    if sql.upper().startswith("CREATE"):
        return None  # CREATE 里的名字是定义而非引用，改名建表仍是合法 SQL。
    for name in model.tables:
        if name in sql:
            return sql.replace(name, "missing_t", 1), "code:UNKNOWN_TABLE"
    return None


def mutate_unknown_column(sql, model):
    if sql.upper().startswith("CREATE"):
        return None  # CREATE 里的列名是定义而非引用。
    for _, columns in model.tables.items():
        for column in columns:
            if column.name in sql:
                return sql.replace(column.name, "missing_c", 1), "code:UNKNOWN_COLUMN"
    return None


def mutate_type_swap(sql, model):
    match = _INT_COMPARISON.search(sql)
    if match:
        swapped = sql[:match.start(3)] + "'x'" + sql[match.end(3):]
        return swapped, "code:TYPE_MISMATCH"
    match = _STR_COMPARISON.search(sql)
    if match:
        swapped = sql[:match.start(3)] + "7" + sql[match.end(3):]
        return swapped, "code:TYPE_MISMATCH"
    match = _INT_LITERAL.search(sql)
    if match and "VALUES" in sql.upper():
        swapped = sql[:match.start(1)] + "'x'" + sql[match.end(1):]
        return swapped, "code:TYPE_MISMATCH"
    return None


def mutate_unterminated_string(sql, model):
    quote = sql.rfind("'")
    if quote < 0:
        return None
    return sql[:quote] + sql[quote + 1:], "code:UNCLOSED_STRING"


def mutate_unclosed_comment(sql, model):
    return sql + "/*", INVALID


def mutate_paren_disturb(sql, model):
    return sql.replace("(", "").replace(")", ""), EITHER


def mutate_token_drop(sql, model):
    parts = sql.split()
    if len(parts) < 2:
        return None
    index = len(parts) // 2  # 确定性选取中间 token，避免总是去掉首尾。
    return " ".join(parts[:index] + parts[index + 1:]), EITHER


def mutate_complexity(sql, model):
    # 构建案例的环境固定有 t0(id INT, name VARCHAR)；由基础语句长度确定嵌套层数，
    # 得到 63/64/65 层括号，结构 token 数恰在 64 边界两侧。
    nesting = 63 + (len(sql) % 3)
    expression = "(" * nesting + "id=1" + ")" * nesting
    tokens = nesting + 1
    expectation = VALID if tokens <= 64 else "code:EXPRESSION_TOO_COMPLEX"
    return f"SELECT * FROM t0 WHERE {expression};", expectation


MUTATORS = (
    mutate_drop_semicolon, mutate_unknown_table, mutate_unknown_column, mutate_type_swap,
    mutate_unterminated_string, mutate_unclosed_comment, mutate_paren_disturb,
    mutate_token_drop, mutate_complexity,
)


# ---------- 案例构造 ----------

def build_case(case_index):
    """每个案例自包含：独立种子流、固定建库、生成基础语句后应用确定性变异。"""
    rng = random.Random(SEED * 1000 + case_index)
    storage, catalog = fresh_db()
    executor = PlanExecutor(storage, catalog)
    for sql in (
        "CREATE TABLE t0(id INT, name VARCHAR);",
        "INSERT INTO t0(id,name) VALUES (1,'a');",
        "INSERT INTO t0(id,name) VALUES (2,'b');",
    ):
        executor.execute(compile_sql(sql, catalog).optimized_plan)
    model = Model()
    model.add("t0", (ColumnSchema("id", DataType.INT), ColumnSchema("name", DataType.VARCHAR)))
    base = gen_statement(rng, model)
    base = "SELECT * FROM t0 WHERE id = 1;" if base is None else base
    compile_sql(base, catalog)  # 基础语句必须合法，否则是生成器缺陷。
    kind = MUTATORS[case_index % len(MUTATORS)]
    outcome = kind(base, model)
    if outcome is None:
        outcome = mutate_drop_semicolon(base, model)  # 兜底：总能应用的确定性变异。
    sql, expectation = outcome
    return kind.__name__, sql, expectation, storage, catalog


def _cases():
    cases = []
    for index in range(MUTATION_CASES):
        kind, sql, expectation, storage, catalog = build_case(index)
        cases.append(pytest.param(
            kind, sql, expectation, storage, catalog,
            id=f"{kind.removeprefix('mutate_')}-{index:03d}",
        ))
    return cases


# ---------- 测试 ----------

def test_generated_valid_statements_never_rejected():
    """固定种子的合法 SQL 序列必须全部编译并执行成功，且优化前后等价。"""
    rng = random.Random(SEED)
    storage, catalog = fresh_db()
    model = Model()
    sql = "CREATE TABLE t0(id INT, name VARCHAR);"
    check_equivalence(sql, storage, catalog, "valid-000")
    model.add("t0", (ColumnSchema("id", DataType.INT), ColumnSchema("name", DataType.VARCHAR)))
    for index in range(1, VALID_STATEMENTS):
        sql = gen_statement(rng, model)
        assert sql is not None, "生成器在非空模型下必须能生成语句"
        try:
            check_equivalence(sql, storage, catalog, f"valid-{index:03d}")
        except MiniSQLError as error:
            raise AssertionError(f"误拒绝 valid-{index:03d}: {sql!r}: {error}") from error


@pytest.mark.parametrize("kind,sql,expectation,storage,catalog", _cases())
def test_mutation_cases(kind, sql, expectation, storage, catalog):
    case_id = kind.removeprefix("mutate_") + f" sql={sql!r} seed={SEED}"
    other_storage, other_catalog = deepcopy(storage), deepcopy(catalog)
    try:
        compiled = compile_sql(sql, catalog)
    except MiniSQLError as error:
        if expectation.startswith("code:"):
            assert error.code == expectation[5:], f"错误码不符 {case_id}: {error}"
        elif expectation == VALID:
            raise AssertionError(f"误拒绝 {case_id}: {error}") from error
        if error.stage in (ErrorStage.LEXICAL, ErrorStage.SYNTAX, ErrorStage.SEMANTIC):
            assert error.position is not None, f"错误缺位置 {case_id}: {error}"
        return
    except BaseException as error:
        raise AssertionError(
            f"未处理异常 {case_id}: {type(error).__name__}: {error}") from error
    if expectation in (INVALID,) or expectation.startswith("code:"):
        raise AssertionError(f"误接受 {case_id}")
    first = execute_plan(compiled.plan, storage, catalog)
    second = execute_plan(compiled.optimized_plan, other_storage, other_catalog)
    if not isinstance(compiled.plan, Explain):
        assert first == second, f"优化前后结果不等 {case_id}"
    assert db_state(storage, catalog) == db_state(other_storage, other_catalog), (
        f"执行后库状态不等 {case_id}")


@pytest.mark.parametrize("nesting,expected_code", [
    (63, None),   # 63 左括号 + 1 个 = 恰好 64，通过
    (64, "EXPRESSION_TOO_COMPLEX"),  # 65 个结构 token，超限
])
def test_complexity_boundary_exact(nesting, expected_code):
    storage, catalog = fresh_db()
    executor = PlanExecutor(storage, catalog)
    executor.execute(compile_sql("CREATE TABLE t0(id INT, name VARCHAR);", catalog).optimized_plan)
    sql = f"SELECT * FROM t0 WHERE {'(' * nesting}id=1{')' * nesting};"
    if expected_code is None:
        check_equivalence(sql, storage, catalog, "boundary")
    else:
        with pytest.raises(MiniSQLError) as error:
            compile_sql(sql, catalog)
        assert error.value.code == expected_code
        assert error.value.position is not None
