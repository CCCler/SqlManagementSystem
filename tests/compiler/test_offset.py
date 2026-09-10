"""LIMIT/OFFSET 编译与语义分析验收。"""
import pytest

from minisql.compiler.compiler import SQLCompiler
from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, TableSchema
from minisql.contracts.plans import Project
from tests.fakes.memory import MemoryCatalog


@pytest.fixture
def catalog():
    return MemoryCatalog((TableSchema("student", (
        ColumnSchema("id", DataType.INT),
        ColumnSchema("name", DataType.VARCHAR)), 7),))


@pytest.fixture
def compile_sql(catalog):
    return lambda sql: SQLCompiler().compile(sql, catalog)


def test_limit_without_offset(compile_sql):
    result = compile_sql("SELECT id FROM student LIMIT 5;")
    assert result.ast.limit == 5
    assert result.ast.offset is None
    assert result.plan.limit == 5
    assert result.plan.offset is None


def test_limit_with_offset(compile_sql):
    result = compile_sql("SELECT id FROM student LIMIT 5 OFFSET 10;")
    assert result.ast.limit == 5
    assert result.ast.offset == 10
    assert result.plan.limit == 5
    assert result.plan.offset == 10


def test_offset_after_order_by(compile_sql):
    result = compile_sql("SELECT id, name FROM student ORDER BY name DESC LIMIT 3 OFFSET 2;")
    plan = result.plan
    assert isinstance(plan, Project)
    assert plan.order_by == (("name", True),)
    assert plan.limit == 3
    assert plan.offset == 2


def test_offset_preserved_after_optimization(compile_sql):
    result = compile_sql("SELECT id FROM student LIMIT 5 OFFSET 3;")
    assert result.optimized_plan.offset == 3


@pytest.mark.parametrize("sql", [
    "SELECT id FROM student OFFSET 3;",  # OFFSET 不能独立出现，必须跟在 LIMIT 后
    "SELECT id FROM student LIMIT;",
    "SELECT id FROM student LIMIT 2 OFFSET;",
    "SELECT id FROM student LIMIT -1;",
])
def test_invalid_limit_offset_syntax(sql, compile_sql):
    with pytest.raises(MiniSQLError) as caught:
        compile_sql(sql)
    assert caught.value.stage is ErrorStage.SYNTAX
