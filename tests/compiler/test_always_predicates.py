"""恒真/恒假计划优化验收。

恒真条件消除 Filter；恒假条件生成 EmptyScan 空结果计划（不扫描用户表），
SELECT 保留正确结果列，DELETE 零影响行；语义检查先行，不因优化绕过错误；
优化保留原计划且不修改原对象，优化前后执行结果等价。"""
import pytest

from minisql.compiler.compiler import SQLCompiler
from minisql.contracts.errors import MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, TableSchema
from minisql.contracts.plans import Delete, EmptyScan, Filter, Project, SeqScan
from minisql.engine.executor import PlanExecutor
from tests.fakes.memory import MemoryCatalog, MemoryStorage


def schema() -> TableSchema:
    return TableSchema("t", (ColumnSchema("id", DataType.INT), ColumnSchema("name", DataType.VARCHAR)))


def make_catalog() -> MemoryCatalog:
    return MemoryCatalog((schema(),))


def compile_sql(sql: str):
    return SQLCompiler().compile(sql, make_catalog())


class CountingStorage(MemoryStorage):
    """统计用户表 scan 调用次数，证明恒假条件不扫描用户表。"""

    def __init__(self):
        super().__init__()
        self.scan_calls = 0

    def scan(self, schema):
        self.scan_calls += 1
        return super().scan(schema)


def make_storage_with_rows(count=3) -> tuple[CountingStorage, MemoryCatalog]:
    storage = CountingStorage()
    table = storage.create_table(schema())
    for i in range(1, count + 1):
        storage.insert(table, (i, f"v{i}"))
    return storage, MemoryCatalog((table,))


def execute(plan, storage, catalog):
    return PlanExecutor(storage, catalog).execute(plan)


@pytest.mark.parametrize("where", [
    "TRUE", "1 = 1", "NOT FALSE", "'a' = 'a'", "id = id OR TRUE",
])
def test_true_predicate_eliminates_filter(where):
    compiled = compile_sql(f"SELECT * FROM t WHERE {where};")
    assert isinstance(compiled.plan.source, Filter)  # 优化前计划保留 Filter
    optimized = compiled.optimized_plan
    assert isinstance(optimized, Project)
    assert isinstance(optimized.source, SeqScan)  # Filter 已消除


@pytest.mark.parametrize("where", [
    "FALSE", "1 = 2", "NOT TRUE", "'a' = 'b'", "id = id AND FALSE",
])
def test_false_predicate_produces_empty_scan(where):
    compiled = compile_sql(f"SELECT * FROM t WHERE {where};")
    optimized = compiled.optimized_plan
    assert isinstance(optimized, Project)
    assert isinstance(optimized.source, EmptyScan)
    assert optimized.source.columns == ("id", "name")


def test_false_predicate_select_keeps_projection_columns():
    compiled = compile_sql("SELECT name FROM t WHERE FALSE;")
    optimized = compiled.optimized_plan
    assert isinstance(optimized, Project)
    assert optimized.columns == ("name",)
    assert isinstance(optimized.source, EmptyScan)
    assert optimized.source.columns == ("id", "name")


def test_true_predicate_delete_scans_and_deletes_all():
    storage, catalog = make_storage_with_rows()
    compiled = SQLCompiler().compile("DELETE FROM t WHERE TRUE;", catalog)
    optimized = compiled.optimized_plan
    assert isinstance(optimized, Delete)
    assert isinstance(optimized.source, SeqScan)  # Filter 已消除，需扫描才能删除
    result = execute(optimized, storage, catalog)
    assert result.affected_rows == 3
    assert storage.scan_calls == 1


def test_false_predicate_delete_zero_effect_without_scan():
    storage, catalog = make_storage_with_rows()
    compiled = SQLCompiler().compile("DELETE FROM t WHERE FALSE;", catalog)
    optimized = compiled.optimized_plan
    assert isinstance(optimized, Delete)
    assert isinstance(optimized.source, EmptyScan)
    result = execute(optimized, storage, catalog)
    assert result.affected_rows == 0
    assert storage.scan_calls == 0  # 证明没有扫描用户表
    assert len(storage.records[next(iter(storage.records))]) == 3  # 记录原样保留


def test_false_predicate_select_does_not_scan_user_table():
    storage, catalog = make_storage_with_rows()
    compiled = SQLCompiler().compile("SELECT * FROM t WHERE FALSE;", catalog)
    result = execute(compiled.optimized_plan, storage, catalog)
    assert result.columns == ("id", "name")
    assert result.rows == ()
    assert storage.scan_calls == 0


@pytest.mark.parametrize("sql,error_code", [
    ("SELECT * FROM t WHERE missing OR TRUE;", "UNKNOWN_COLUMN"),
    ("SELECT * FROM missing WHERE TRUE;", "UNKNOWN_TABLE"),
    ("SELECT * FROM t WHERE id = 'a' OR TRUE;", "TYPE_MISMATCH"),
    ("SELECT * FROM t WHERE 1 + 2;", "TYPE_MISMATCH"),
])
def test_semantic_errors_not_bypassed_by_optimization(sql, error_code):
    with pytest.raises(MiniSQLError) as error:
        compile_sql(sql)
    assert error.value.code == error_code


def test_original_plan_kept_and_not_modified():
    compiled = compile_sql("SELECT * FROM t WHERE TRUE;")
    original = compiled.plan
    assert isinstance(original.source, Filter)  # 优化前计划仍保留 Filter
    assert original.source.predicate.value is True
    optimized = compiled.optimized_plan
    assert optimized is not original
    assert not isinstance(optimized.source, Filter)
    # 重复优化结果一致，原计划不受影响。
    assert compiled.optimized_plan == optimized
    assert original.source.predicate.value is True


@pytest.mark.parametrize("sql", [
    "SELECT * FROM t WHERE TRUE;",
    "SELECT * FROM t WHERE FALSE;",
    "SELECT name FROM t WHERE 1 = 1;",
    "SELECT name FROM t WHERE 1 = 2;",
    "SELECT * FROM t WHERE id = id OR TRUE;",
    "SELECT * FROM t WHERE id = id AND FALSE;",
])
def test_optimized_plan_equivalent_to_original(sql):
    storage, catalog = make_storage_with_rows()
    compiled = SQLCompiler().compile(sql, catalog)
    original = execute(compiled.plan, storage, catalog)
    optimized = execute(compiled.optimized_plan, storage, catalog)
    assert optimized.columns == original.columns
    assert optimized.rows == original.rows


def test_explain_renders_empty_scan():
    storage, catalog = make_storage_with_rows()
    compiled = SQLCompiler().compile("EXPLAIN SELECT * FROM t WHERE FALSE;", catalog)
    result = execute(compiled.optimized_plan, storage, catalog)
    assert "EmptyScan(id, name)" in result.message
    assert storage.scan_calls == 0
