"""独立编译器验收，不依赖执行器。"""
from copy import deepcopy
from dataclasses import replace
import pytest
from minisql.compiler.compiler import SQLCompiler
from minisql.compiler.lexer import Lexer
from minisql.compiler.parser import Parser
from minisql.compiler.semantic import SemanticAnalyzer
from minisql.compiler.planner import Planner
from minisql.compiler.optimizer import Optimizer
from minisql.contracts.ast import (
    BinaryExpr, CreateTableStmt, DeleteStmt, Identifier, InsertStmt, Literal, SelectStmt, UnaryExpr,
)
from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, SourcePosition, TableSchema, TokenType
from minisql.contracts.plans import CreateTable, Delete, Filter, Insert, Project, SeqScan
from tests.fakes.memory import MemoryCatalog


@pytest.fixture
def catalog():
    return MemoryCatalog((TableSchema("student", (
        ColumnSchema("id", DataType.INT), ColumnSchema("name", DataType.VARCHAR)), 7),))


@pytest.fixture
def compile_sql(catalog):
    return lambda sql: SQLCompiler().compile(sql, catalog)


@pytest.mark.parametrize("sql,ast_type,plan_type", [
    ("CREATE TABLE other (id INT, name VARCHAR);", CreateTableStmt, CreateTable),
    ("INSERT INTO student (name,id) VALUES ('中文',3);", InsertStmt, Insert),
    ("SELECT * FROM student;", SelectStmt, Project),
    ("DELETE FROM student;", DeleteStmt, Delete),
])
def test_four_statement_ast(sql, ast_type, plan_type, compile_sql, catalog):
    before = deepcopy(catalog.tables)
    result = compile_sql(sql)
    assert isinstance(result.ast, ast_type)
    assert isinstance(result.plan, plan_type)
    assert result.tokens[-1].type is TokenType.EOF
    assert result.semantic.statement == result.ast
    assert result.optimized_plan == result.plan
    assert result.optimized_plan is not result.plan
    assert catalog.tables == before


def test_individual_stages_and_insert_mapping(catalog):
    tokens = Lexer().tokenize("iNsErT INTO STUDENT (NAME,ID) VALUES ('O''Brien;中文',-2);")
    ast = Parser().parse(tokens)
    semantic = SemanticAnalyzer().analyze(ast, catalog)
    plan = Planner().build(semantic)
    assert tokens[0].lexeme == "iNsErT"
    assert ast.table.name == "student"
    assert plan.values == (-2, "O'Brien;中文")
    assert plan.schema.table_id == 7
    assert Optimizer().optimize(plan) == plan


def test_token_file_position():
    tokens = Lexer().tokenize("-- 注释\r\n  SeLeCt 'a'';中', 12 <= 13 /* x\ny */\n;")
    assert [(t.lexeme, t.position) for t in tokens] == [
        ("SeLeCt", SourcePosition(2, 3)), ("'a'';中'", SourcePosition(2, 10)),
        (",", SourcePosition(2, 17)), ("12", SourcePosition(2, 19)),
        ("<=", SourcePosition(2, 22)), ("13", SourcePosition(2, 25)),
        (";", SourcePosition(4, 1)), ("", SourcePosition(4, 2)),
    ]
    assert tokens[1].type is TokenType.CONST
    assert tokens[4].type is TokenType.OPERATOR


def test_comments_strings_semicolon(compile_sql):
    sql = "-- ;\nINSERT INTO student (id,name) VALUES (1,'a'';中'); /* ; */\nSELECT name FROM student; -- tail;"
    pieces = SQLCompiler().split_statements(sql)
    assert len(pieces) == 2
    assert compile_sql(pieces[0]).plan.values == (1, "a';中")
    assert compile_sql(pieces[1]).ast.position == SourcePosition(3, 1)
    assert pieces[1].index("SELECT") == sql.index("SELECT")


@pytest.mark.parametrize("sql", ["", " \t\r\n", "-- ;", "/* ; */\n-- ';"])
def test_empty_or_comments_split(sql):
    assert SQLCompiler().split_statements(sql) == ()


@pytest.mark.parametrize("tail,stage,code", [
    ("SELECT * FROM student", ErrorStage.SYNTAX, "UNEXPECTED_TOKEN"),
    ("SELECT * FROM student WHERE name='oops", ErrorStage.LEXICAL, "UNCLOSED_STRING"),
    ("/* missing", ErrorStage.LEXICAL, "UNCLOSED_COMMENT"),
    ("@", ErrorStage.LEXICAL, "INVALID_CHARACTER"),
])
def test_late_error_preserved(tail, stage, code, compile_sql):
    pieces = SQLCompiler().split_statements("SELECT * FROM student;\n" + tail)
    assert len(pieces) == 2
    compile_sql(pieces[0])
    with pytest.raises(MiniSQLError) as caught:
        compile_sql(pieces[1])
    assert caught.value.stage is stage
    assert caught.value.code == code
    assert caught.value.position.line == 2


def test_not_and_or_precedence(compile_sql):
    expr = compile_sql("SELECT * FROM student WHERE NOT id + 2 = 3 AND TRUE OR FALSE;").ast.where
    assert expr.operator == "OR"
    assert expr.left.operator == "AND"
    assert expr.left.left.operator == "NOT"
    assert expr.left.left.operand.operator == "="
    assert expr.left.left.operand.left.operator == "+"
    expr = compile_sql("SELECT * FROM student WHERE (id - 2 - 3) = -4;").ast.where
    assert expr.left.operator == "-"
    assert expr.left.left.operator == "-"
    assert expr.right.value == -4


def test_select_and_delete_plan_shapes(compile_sql):
    all_columns = compile_sql("SELECT * FROM student;").plan
    assert all_columns.columns == ("id", "name")
    assert isinstance(all_columns.source, SeqScan)
    selected = compile_sql("SELECT name, id, name FROM student WHERE id >= 2;").plan
    assert selected.columns == ("name", "id", "name")
    assert isinstance(selected.source, Filter)
    assert selected.source.source == all_columns.source
    deleted = compile_sql("DELETE FROM student WHERE id <> 2;").plan
    assert isinstance(deleted, Delete)
    assert isinstance(deleted.source, Filter)
    assert isinstance(deleted.source.source, SeqScan)
    assert isinstance(compile_sql("DELETE FROM student;").plan.source, SeqScan)


@pytest.mark.parametrize("sql,code,position", [
    ("\n @", "INVALID_CHARACTER", SourcePosition(2, 2)),
    ("SELECT 'oops", "UNCLOSED_STRING", SourcePosition(1, 8)),
    ("\n/*oops", "UNCLOSED_COMMENT", SourcePosition(2, 1)),
    ("SELECT 中 FROM student;", "INVALID_CHARACTER", SourcePosition(1, 8)),
    ("SELECT * FROM student WHERE id ! 1;", "INVALID_CHARACTER", SourcePosition(1, 32)),
])
def test_lexical_errors(sql, code, position, compile_sql):
    with pytest.raises(MiniSQLError) as caught:
        compile_sql(sql)
    assert caught.value.stage is ErrorStage.LEXICAL
    assert caught.value.code == code
    assert caught.value.position == position


@pytest.mark.parametrize("sql", [
    "SELECT * FROM student", "SELECT * FROM student WHERE (id=1;",
    "SELECT * FROM student WHERE id < 1 < 2;", "SELECT * FROM student;;",
    "SELECT * FROM student; SELECT * FROM student;", "CREATE TABLE t ();",
    "CREATE TABLE t (a VARCHAR(10));", "CREATE TABLE t (a BOOL);",
    "INSERT INTO student VALUES (1,'x');", "INSERT INTO student (id,name) VALUES (1+2,'x');",
    "INSERT INTO student (id,name) VALUES (-TRUE,'x');", "SELECT * FROM student WHERE -id=1;",
    "SELECT * FROM student WHERE id=1.2;",
    "SELECT * FROM student WHERE id * 2=4;", "SELECT * FROM student WHERE id=+1;",
    "UPDATE student;", "SELECT 'FROM' student;", "SELECT name, FROM student;", "", ";",
])
def test_invalid_syntax(sql, compile_sql):
    with pytest.raises(MiniSQLError) as caught:
        compile_sql(sql)
    assert caught.value.stage in (ErrorStage.SYNTAX, ErrorStage.LEXICAL)
    assert caught.value.position is not None
    if caught.value.stage is ErrorStage.SYNTAX:
        assert caught.value.expected


def test_missing_semicolon_exact_location(compile_sql):
    with pytest.raises(MiniSQLError) as caught:
        compile_sql("SELECT * FROM student\n")
    assert caught.value.position == SourcePosition(2, 1)
    assert caught.value.expected == (";",)


@pytest.mark.parametrize("sql,code", [
    ("CREATE TABLE STUDENT (x INT);", "DUPLICATE_TABLE"),
    ("CREATE TABLE t (a INT,A VARCHAR);", "DUPLICATE_COLUMN"),
    ("SELECT * FROM missing;", "UNKNOWN_TABLE"),
    ("DELETE FROM missing;", "UNKNOWN_TABLE"),
    ("INSERT INTO missing (id) VALUES (1);", "UNKNOWN_TABLE"),
    ("SELECT missing FROM student;", "UNKNOWN_COLUMN"),
    ("SELECT * FROM student WHERE id=NULL;", "UNKNOWN_COLUMN"),
    ("DELETE FROM student WHERE missing=1;", "UNKNOWN_COLUMN"),
    ("INSERT INTO student (id,missing) VALUES (1,'x');", "UNKNOWN_COLUMN"),
    ("INSERT INTO student (id,ID) VALUES (1,2);", "DUPLICATE_COLUMN"),
    ("INSERT INTO student (id) VALUES (1);", "MISSING_COLUMN"),
    ("INSERT INTO student (id,name) VALUES (1);", "VALUE_COUNT_MISMATCH"),
    ("INSERT INTO student (id,name) VALUES (1,'x',2);", "VALUE_COUNT_MISMATCH"),
    ("INSERT INTO student (id,name) VALUES (TRUE,'x');", "TYPE_MISMATCH"),
    ("INSERT INTO student (id,name) VALUES ('1','x');", "TYPE_MISMATCH"),
    ("INSERT INTO student (id,name) VALUES (1,2);", "TYPE_MISMATCH"),
    ("SELECT * FROM student WHERE 1;", "TYPE_MISMATCH"),
    ("SELECT * FROM student WHERE name;", "TYPE_MISMATCH"),
    ("SELECT * FROM student WHERE id=TRUE;", "TYPE_MISMATCH"),
    ("SELECT * FROM student WHERE id=name;", "TYPE_MISMATCH"),
    ("SELECT * FROM student WHERE name+1=2;", "TYPE_MISMATCH"),
    ("SELECT * FROM student WHERE TRUE+1=2;", "TYPE_MISMATCH"),
    ("SELECT * FROM student WHERE NOT id;", "TYPE_MISMATCH"),
    ("SELECT * FROM student WHERE TRUE AND id;", "TYPE_MISMATCH"),
    ("SELECT * FROM student WHERE TRUE OR missing=1;", "UNKNOWN_COLUMN"),
])
def test_semantic_errors(sql, code, compile_sql):
    with pytest.raises(MiniSQLError) as caught:
        compile_sql(sql)
    assert caught.value.stage is ErrorStage.SEMANTIC
    assert caught.value.code == code
    assert caught.value.position is not None


@pytest.mark.parametrize("value", [-(2 ** 63), 2 ** 63 - 1, 0])
def test_integer_boundaries(value, compile_sql):
    assert compile_sql(f"INSERT INTO student (id,name) VALUES ({value},'');").plan.values == (value, "")


@pytest.mark.parametrize("value", [str(2 ** 63), str(-(2 ** 63) - 1), "9" * 5000])
def test_out_of_range_integer(value, compile_sql):
    with pytest.raises(MiniSQLError) as caught:
        compile_sql(f"SELECT * FROM student WHERE id={value};")
    assert caught.value.code == "INTEGER_OUT_OF_RANGE"


def test_analyzer_normalizes_direct_ast_and_rejects_bool_int(catalog):
    ast = Parser().parse(Lexer().tokenize("SELECT id FROM student WHERE id=1;"))
    ast = replace(ast, table=replace(ast.table, name="STUDENT"),
                  columns=(replace(ast.columns[0], name="ID"),))
    assert SemanticAnalyzer().analyze(ast, catalog).statement.table.name == "student"
    bad = replace(ast, where=Literal(True, DataType.INT, SourcePosition(1, 1)))
    with pytest.raises(MiniSQLError, match="TYPE_MISMATCH"):
        SemanticAnalyzer().analyze(bad, catalog)


def evaluate(expr, row):
    """测试用参考语义，不调用优化器内部函数。"""
    if isinstance(expr, Literal):
        return expr.value
    if isinstance(expr, Identifier):
        return row[expr.name]
    if isinstance(expr, UnaryExpr):
        return not evaluate(expr.operand, row)
    left = evaluate(expr.left, row)
    if expr.operator == "AND":
        return left and evaluate(expr.right, row)
    if expr.operator == "OR":
        return left or evaluate(expr.right, row)
    right = evaluate(expr.right, row)
    if expr.operator == "+":
        return left + right
    if expr.operator == "-":
        return left - right
    if expr.operator == "=":
        return left == right
    if expr.operator in ("!=", "<>"):
        return left != right
    if expr.operator == "<":
        return left < right
    if expr.operator == "<=":
        return left <= right
    if expr.operator == ">":
        return left > right
    if expr.operator == ">=":
        return left >= right
    raise AssertionError(expr.operator)


@pytest.mark.parametrize("predicate", [
    "id=1+2-1", "TRUE AND id=2", "id=2 AND TRUE", "FALSE OR id=2", "id=2 OR FALSE",
    "FALSE AND id=2", "id=2 AND FALSE", "TRUE OR id=2", "id=2 OR TRUE",
    "NOT NOT id=2", "NOT (TRUE AND FALSE)", "1+2=3 AND id=2",
    "(1=2 OR name='中文') AND NOT (id<0)", "'a'<'b' OR FALSE",
    "1 != 2", "1 <> 2", "1 <= 2", "2 > 1", "2 >= 2", "TRUE=FALSE",
    "(TRUE AND id=1) OR (FALSE OR name='x')",
])
@pytest.mark.parametrize("statement", ["SELECT * FROM student", "DELETE FROM student"])
def test_optimization_equivalence(predicate, statement, compile_sql):
    result = compile_sql(f"{statement} WHERE {predicate};")
    before = deepcopy(result.plan)
    original = result.plan.source.predicate
    optimized = result.optimized_plan.source.predicate
    for row_id in (-(2 ** 63), -2, 0, 1, 2, 3, 2 ** 63 - 1):
        for name in ("", "x", "中文"):
            row = {"id": row_id, "name": name}
            assert evaluate(original, row) == evaluate(optimized, row)
    assert optimized != original
    assert Optimizer().optimize(result.plan) == result.optimized_plan
    assert result.plan == before
    assert Optimizer().optimize(result.optimized_plan) == result.optimized_plan


def test_folding_preserves_out_of_range_expression(compile_sql):
    result = compile_sql(f"SELECT * FROM student WHERE {2 ** 63 - 1}+1>0;")
    assert result.optimized_plan == result.plan
    assert isinstance(result.optimized_plan.source.predicate.left, BinaryExpr)


def test_folding_has_expected_value_and_position(compile_sql):
    result = compile_sql("SELECT * FROM student WHERE 1+2=3;")
    assert result.optimized_plan.source.predicate == Literal(True, DataType.BOOL, result.ast.where.position)


def test_parser_reusable_and_missing_eof():
    parser = Parser()
    tokens = Lexer().tokenize("SELECT * FROM student;")
    assert parser.parse(tokens) == parser.parse(tokens)
    for invalid in ((), tokens[:-1]):
        with pytest.raises(MiniSQLError) as caught:
            parser.parse(invalid)
        assert caught.value.expected == ("EOF",)


@pytest.mark.parametrize("sql,position", [
    ("\nSELECT missing FROM student;", SourcePosition(2, 8)),
    ("\nSELECT * FROM missing;", SourcePosition(2, 15)),
    ("\nCREATE TABLE t (id INT, ID INT);", SourcePosition(2, 1)),
    ("\nSELECT * FROM student WHERE id='x';", SourcePosition(2, 31)),
])
def test_semantic_error_positions(sql, position, compile_sql):
    with pytest.raises(MiniSQLError) as caught:
        compile_sql(sql)
    assert caught.value.position == position


def test_multiline_strings_and_same_line_fragments(compile_sql):
    sql = "INSERT INTO student (id,name) VALUES (1,'line1\nline2'); SELECT ID FROM STUDENT;"
    pieces = SQLCompiler().split_statements(sql)
    assert len(pieces) == 2
    assert compile_sql(pieces[0]).plan.values == (1, "line1\nline2")
    second = compile_sql(pieces[1])
    assert second.ast.position == SourcePosition(2, 10)
    assert second.plan.columns == ("id",)


def test_optimizer_does_not_mutate_input(catalog):
    ast = Parser().parse(Lexer().tokenize("SELECT * FROM student WHERE id=1+2 AND TRUE;"))
    plan = Planner().build(SemanticAnalyzer().analyze(ast, catalog))
    before = deepcopy(plan)
    optimized = Optimizer().optimize(plan)
    assert plan == before
    assert optimized != before
    assert optimized.source.predicate.right.value == 3


def test_all_operator_tokens():
    operators = "= != <> < <= > >= + - *"
    tokens = Lexer().tokenize(operators)
    assert [t.lexeme for t in tokens[:-1]] == operators.split()
    assert all(t.type is TokenType.OPERATOR for t in tokens[:-1])


def test_long_leading_zero_integer(compile_sql):
    result = compile_sql("INSERT INTO student (id,name) VALUES (" + "0" * 5000 + "1,'');")
    assert result.plan.values == (1, "")


def test_create_names_and_schema_identity(compile_sql):
    result = compile_sql("CREATE TABLE My_Table (Some_ID INT, Display_Name VARCHAR);")
    assert result.plan.schema.name == "my_table"
    assert tuple(c.name for c in result.plan.schema.columns) == ("some_id", "display_name")
    assert result.plan.schema.table_id is None


def test_generated_boolean_equivalence(compile_sql):
    import random
    randomizer = random.Random(42)
    atoms = ["TRUE", "FALSE", "id=2", "id<0", "name='中文'", "1+2=3"]
    predicates = atoms[:]
    for _ in range(50):
        left, right = randomizer.choices(atoms, k=2)
        predicates.append(f"NOT ({left}) {randomizer.choice(['AND', 'OR'])} ({right})")
    for predicate in predicates:
        result = compile_sql(f"SELECT * FROM student WHERE {predicate};")
        for row_id in range(-3, 4):
            for name in ("", "中文"):
                row = {"id": row_id, "name": name}
                assert evaluate(result.plan.source.predicate, row) == evaluate(result.optimized_plan.source.predicate, row)
