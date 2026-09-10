"""ORDER BY 编译与语义分析验收。"""
import pytest

from minisql.compiler.compiler import SQLCompiler
from minisql.contracts.ast import OrderTerm, SelectStmt
from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, TableSchema
from minisql.contracts.plans import Project
from tests.fakes.memory import MemoryCatalog


@pytest.fixture
def catalog():
    return MemoryCatalog((TableSchema("student", (
        ColumnSchema("id", DataType.INT),
        ColumnSchema("name", DataType.VARCHAR),
        ColumnSchema("score", DataType.INT)), 7),))


@pytest.fixture
def compile_sql(catalog):
    return lambda sql: SQLCompiler().compile(sql, catalog)


def test_order_by_single_column_default_ascending(compile_sql):
    result = compile_sql("SELECT id, name FROM student ORDER BY name;")
    assert isinstance(result.ast, SelectStmt)
    assert len(result.ast.order_by) == 1
    term = result.ast.order_by[0]
    assert isinstance(term, OrderTerm)
    assert term.column.name == "name"
    assert term.descending is False
    assert result.plan.order_by == (("name", False),)


def test_order_by_multiple_columns_mixed_directions(compile_sql):
    result = compile_sql("SELECT id, name, score FROM student ORDER BY name DESC, id ASC, score DESC;")
    assert [t.column.name for t in result.ast.order_by] == ["name", "id", "score"]
    assert [t.descending for t in result.ast.order_by] == [True, False, True]
    assert result.plan.order_by == (("name", True), ("id", False), ("score", True))


def test_order_by_star_uses_all_columns(compile_sql):
    result = compile_sql("SELECT * FROM student ORDER BY score DESC;")
    assert result.ast.columns is None
    assert result.plan.order_by == (("score", True),)


def test_order_by_plan_shape(compile_sql):
    result = compile_sql("SELECT name FROM student WHERE id > 1 ORDER BY name DESC LIMIT 5;")
    plan = result.plan
    assert isinstance(plan, Project)
    assert plan.columns == ("name",)
    assert plan.order_by == (("name", True),)
    assert plan.limit == 5


def test_order_by_preserved_after_optimization(compile_sql):
    result = compile_sql("SELECT name FROM student ORDER BY name;")
    assert result.optimized_plan.order_by == (("name", False),)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM student ORDER BY missing;",
    "SELECT id FROM student ORDER BY name;",
    "SELECT id, name FROM student ORDER BY score;",
])
def test_order_by_unknown_or_unprojected_column(sql, compile_sql):
    with pytest.raises(MiniSQLError) as caught:
        compile_sql(sql)
    assert caught.value.stage is ErrorStage.SEMANTIC
    assert caught.value.code == "UNKNOWN_COLUMN"


@pytest.mark.parametrize("sql", [
    "SELECT id FROM student ORDER BY;",
    "SELECT id FROM student ORDER id;",
    "SELECT id FROM student ORDER BY id id;",
    "SELECT id FROM student ORDER BY id DESC name;",
])
def test_order_by_invalid_syntax(sql, compile_sql):
    with pytest.raises(MiniSQLError) as caught:
        compile_sql(sql)
    assert caught.value.stage is ErrorStage.SYNTAX
