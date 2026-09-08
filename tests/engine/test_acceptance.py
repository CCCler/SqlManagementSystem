"""引擎验收：人工计划 + MemoryStorage/MemoryCatalog 验证算子与目录，不依赖编译器与页存储。"""
from dataclasses import replace

import pytest

from minisql.contracts.ast import BinaryExpr, Identifier, Literal
from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, SourcePosition, TableSchema
from minisql.contracts.plans import CreateTable, Delete, Filter, Insert, Project, SeqScan
from minisql.engine.catalog import PersistentCatalog
from minisql.engine.executor import PlanExecutor
from tests.fakes.memory import MemoryCatalog, MemoryStorage

POS = SourcePosition(1, 1)


def student_schema() -> TableSchema:
    return TableSchema(
        "student",
        (
            ColumnSchema("id", DataType.INT),
            ColumnSchema("name", DataType.VARCHAR),
            ColumnSchema("age", DataType.INT),
        ),
    )


def test_manual_create_insert_select_delete():
    storage = MemoryStorage()
    executor = PlanExecutor(storage, MemoryCatalog())
    created = executor.execute(CreateTable(student_schema()))
    assert created.message == "表 student 已创建"
    schema = executor.catalog.get_table("student")
    executor.execute(Insert(schema, (1, "Alice", 20)))
    executor.execute(Insert(schema, (2, "Bob", 17)))
    scan = executor.execute(SeqScan(schema))
    assert scan.columns == ("id", "name", "age")
    assert scan.rows == ((1, "Alice", 20), (2, "Bob", 17))
    # SELECT id,name FROM student WHERE age > 18 → 只有 Alice
    age_pred = BinaryExpr(">", Identifier("age", POS), Literal(18, DataType.INT, POS), POS)
    query = executor.execute(Project(("id", "name"), Filter(age_pred, SeqScan(schema))))
    assert query.columns == ("id", "name")
    assert query.rows == ((1, "Alice"),)
    # DELETE FROM student WHERE id = 1
    id_pred = BinaryExpr("=", Identifier("id", POS), Literal(1, DataType.INT, POS), POS)
    deleted = executor.execute(Delete(schema, Filter(id_pred, SeqScan(schema))))
    assert deleted.affected_rows == 1
    assert executor.execute(SeqScan(schema)).rows == ((2, "Bob", 17),)


def test_project_selects_columns():
    storage = MemoryStorage()
    executor = PlanExecutor(storage, MemoryCatalog())
    table = storage.create_table(student_schema())
    executor.execute(Insert(table, (1, "Alice", 20)))
    projected = executor.execute(Project(("name", "age"), SeqScan(table)))
    assert projected.columns == ("name", "age")
    assert projected.rows == (("Alice", 20),)


def test_filter_retains_record_id():
    storage = MemoryStorage()
    executor = PlanExecutor(storage, MemoryCatalog())
    table = storage.create_table(TableSchema("ids", (ColumnSchema("id", DataType.INT),)))
    for value in (1, 2, 3):
        executor.execute(Insert(table, (value,)))
    predicate = BinaryExpr("=", Identifier("id", POS), Literal(2, DataType.INT, POS), POS)
    deleted = executor.execute(Delete(table, Filter(predicate, SeqScan(table))))
    assert deleted.affected_rows == 1
    assert executor.execute(SeqScan(table)).rows == ((1,), (3,))


def test_persistent_catalog_bootstrap_and_reload():
    storage = MemoryStorage()
    catalog = PersistentCatalog(storage)
    catalog.bootstrap()
    table = storage.create_table(student_schema())
    assert table.table_id == 1
    catalog.register_table(table)

    expected = replace(table, name="student")
    assert catalog.get_table("STUDENT") == expected
    assert catalog.list_tables() == (expected,)

    reloaded = PersistentCatalog(storage)
    reloaded.bootstrap()
    assert reloaded.get_table("student") == expected
    assert reloaded.list_tables() == (expected,)
    # bootstrap 幂等：重复初始化不破坏目录
    reloaded.bootstrap()
    assert reloaded.get_table("student") == expected


def test_persistent_catalog_rejects_duplicate_table():
    storage = MemoryStorage()
    catalog = PersistentCatalog(storage)
    catalog.bootstrap()
    table = storage.create_table(student_schema())
    catalog.register_table(table)
    with pytest.raises(MiniSQLError) as error:
        catalog.register_table(replace(table, table_id=2))
    assert error.value.code == "DUPLICATE_TABLE"


def test_unknown_plan_raises_execution_error():
    executor = PlanExecutor(MemoryStorage(), MemoryCatalog())
    with pytest.raises(MiniSQLError) as error:
        executor.execute(object())  # type: ignore[arg-type]
    assert error.value.stage is ErrorStage.EXECUTION
    assert error.value.code == "UNKNOWN_PLAN"


def test_filter_rejects_non_boolean_predicate():
    storage = MemoryStorage()
    executor = PlanExecutor(storage, MemoryCatalog())
    table = storage.create_table(TableSchema("ids", (ColumnSchema("id", DataType.INT),)))
    executor.execute(Insert(table, (1,)))
    predicate = BinaryExpr("+", Identifier("id", POS), Literal(1, DataType.INT, POS), POS)
    with pytest.raises(MiniSQLError) as error:
        executor.execute(Filter(predicate, SeqScan(table)))
    assert error.value.code == "TYPE_MISMATCH"


def test_bool_is_not_treated_as_int():
    storage = MemoryStorage()
    executor = PlanExecutor(storage, MemoryCatalog())
    table = storage.create_table(TableSchema("ids", (ColumnSchema("id", DataType.INT),)))
    executor.execute(Insert(table, (1,)))
    predicate = BinaryExpr("=", Identifier("id", POS), Literal(True, DataType.BOOL, POS), POS)
    with pytest.raises(MiniSQLError) as error:
        executor.execute(Filter(predicate, SeqScan(table)))
    assert error.value.code == "TYPE_MISMATCH"


def test_project_unknown_column_raises():
    storage = MemoryStorage()
    executor = PlanExecutor(storage, MemoryCatalog())
    table = storage.create_table(student_schema())
    with pytest.raises(MiniSQLError) as error:
        executor.execute(Project(("missing",), SeqScan(table)))
    assert error.value.code == "UNKNOWN_COLUMN"


@pytest.mark.parametrize("operator", ["<", "<=", ">", ">="])
@pytest.mark.parametrize("left,right,left_type,right_type", [
    (1, "1", DataType.INT, DataType.VARCHAR),
    (1, True, DataType.INT, DataType.BOOL),
    ("1", False, DataType.VARCHAR, DataType.BOOL),
])
def test_ordering_rejects_mixed_types(operator, left, right, left_type, right_type):
    storage = MemoryStorage()
    executor = PlanExecutor(storage, MemoryCatalog())
    table = storage.create_table(student_schema())
    executor.execute(Insert(table, (1, "Alice", 20)))
    predicate = BinaryExpr(operator, Literal(left, left_type, POS), Literal(right, right_type, POS), POS)
    with pytest.raises(MiniSQLError) as error:
        executor.execute(Filter(predicate, SeqScan(table)))
    assert error.value.code == "TYPE_MISMATCH"


@pytest.mark.parametrize("operator,expected", [("<", True), ("<=", True), (">", False), (">=", False)])
def test_boolean_ordering_matches_compiler(operator, expected):
    storage = MemoryStorage()
    executor = PlanExecutor(storage, MemoryCatalog())
    table = storage.create_table(student_schema())
    executor.execute(Insert(table, (1, "Alice", 20)))
    predicate = BinaryExpr(operator, Literal(False, DataType.BOOL, POS), Literal(True, DataType.BOOL, POS), POS)
    result = executor.execute(Filter(predicate, SeqScan(table)))
    assert result.rows == (((1, "Alice", 20),) if expected else ())
