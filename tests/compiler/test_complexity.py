import pytest

from minisql.compiler.compiler import SQLCompiler
from minisql.compiler.parser import MAX_EXPRESSION_COMPLEXITY
from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, TableSchema
from tests.fakes.memory import MemoryCatalog


def compile_where(expression):
    catalog = MemoryCatalog((TableSchema("t", (ColumnSchema("id", DataType.INT),), 1),))
    return SQLCompiler().compile("SELECT * FROM t WHERE " + expression + ";", catalog)


@pytest.mark.parametrize("expression", [
    "(" * 300 + "id=1" + ")" * 300,
    "(" * 300 + "id=1",
    "NOT " * 1200 + "id=1",
    " AND ".join(["id=1"] * 1200),
    "+".join(["1"] * 1200) + "=id",
    "NOT (" * 200 + "TRUE" + ")" * 200,
])
def test_deep_expressions_return_positioned_error(expression):
    with pytest.raises(MiniSQLError) as caught:
        compile_where(expression)
    error = caught.value
    assert error.stage is ErrorStage.SYNTAX
    assert error.code == "EXPRESSION_TOO_COMPLEX"
    assert error.position.line == 1 and error.position.column > 22
    assert error.expected == ("SIMPLER_EXPRESSION",)


@pytest.mark.parametrize("expression", [
    "(" * MAX_EXPRESSION_COMPLEXITY + "TRUE" + ")" * MAX_EXPRESSION_COMPLEXITY,
    "NOT " * MAX_EXPRESSION_COMPLEXITY + "TRUE",
    "+".join(["1"] * MAX_EXPRESSION_COMPLEXITY) + "=64",
    " AND ".join(["TRUE"] * (MAX_EXPRESSION_COMPLEXITY + 1)),
])
def test_exact_budget_accepts_complete_pipeline(expression):
    assert compile_where(expression).optimized_plan is not None


def test_limit_position_preserves_file_lines():
    sql = "-- header\nSELECT * FROM t WHERE\n" + "(" * 65 + "TRUE" + ")" * 65 + ";"
    catalog = MemoryCatalog((TableSchema("t", (ColumnSchema("id", DataType.INT),), 1),))
    with pytest.raises(MiniSQLError) as caught:
        SQLCompiler().compile(sql, catalog)
    assert (caught.value.position.line, caught.value.position.column) == (3, 65)


def test_string_and_comment_contents_do_not_use_budget():
    text = "NOT ( + AND " * 500
    assert compile_where(f"'{text}' = '{text}' /* {text} */").optimized_plan is not None


def test_shallow_unclosed_parenthesis_remains_syntax_error():
    with pytest.raises(MiniSQLError) as caught:
        compile_where("(id=1")
    assert caught.value.code == "UNEXPECTED_TOKEN"
    assert caught.value.expected == (")",)
