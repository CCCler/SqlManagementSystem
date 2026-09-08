"""DROP TABLE 的编译、目录持久化、页复用和命令行回归。"""
from dataclasses import replace

import pytest

from minisql.cli.main import main
from minisql.compiler.compiler import SQLCompiler
from minisql.contracts.ast import DropTableStmt
from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, SourcePosition, TableSchema
from minisql.contracts.plans import DropTable
from minisql.engine.catalog import SYSTEM_CATALOG
from minisql.engine.database import open_database
from minisql.engine.executor import PlanExecutor
from minisql.storage.buffer import PageBufferPool
from minisql.storage.file_manager import FileManager
from minisql.storage.page import DiskPageManager, decode_meta
from minisql.storage.record import HeapStorage
from tests.fakes.memory import MemoryCatalog, MemoryStorage


def test_drop_compiler_is_read_only():
    schema = TableSchema("student", (ColumnSchema("id", DataType.INT),), 1)
    catalog = MemoryCatalog((schema,))
    compiled = SQLCompiler().compile("\nDrOp TABLE STUDENT;", catalog)
    assert isinstance(compiled.ast, DropTableStmt)
    assert compiled.ast.table.name == "student"
    assert compiled.ast.position == SourcePosition(2, 1)
    assert compiled.plan == compiled.optimized_plan == DropTable(schema)
    assert catalog.get_table("student") == schema


@pytest.mark.parametrize("sql,stage,code", [
    ("DROP TABLE missing;", ErrorStage.SEMANTIC, "UNKNOWN_TABLE"),
    ("DROP TABLE __CATALOG;", ErrorStage.SEMANTIC, "PROTECTED_TABLE"),
    ("DROP missing;", ErrorStage.SYNTAX, "UNEXPECTED_TOKEN"),
    ("DROP TABLE;", ErrorStage.SYNTAX, "UNEXPECTED_TOKEN"),
    ("DROP TABLE missing", ErrorStage.SYNTAX, "UNEXPECTED_TOKEN"),
    ("DROP TABLE a,b;", ErrorStage.SYNTAX, "UNEXPECTED_TOKEN"),
    ("DROP TABLE a WHERE TRUE;", ErrorStage.SYNTAX, "UNEXPECTED_TOKEN"),
])
def test_drop_errors(sql, stage, code):
    with pytest.raises(MiniSQLError) as error:
        SQLCompiler().compile(sql, MemoryCatalog())
    assert error.value.stage is stage
    assert error.value.code == code
    assert error.value.position is not None


def test_drop_memory_executor_and_stale_plan():
    storage = MemoryStorage()
    catalog = MemoryCatalog()
    executor = PlanExecutor(storage, catalog)
    schema = storage.create_table(TableSchema("t", (ColumnSchema("id", DataType.INT),)))
    catalog.register_table(schema)
    plan = DropTable(schema)
    assert executor.execute(plan).message == "表 t 已删除"
    assert catalog.list_tables() == ()
    with pytest.raises(MiniSQLError, match="UNKNOWN_TABLE"):
        list(storage.scan(schema))
    new_schema = storage.create_table(replace(schema, table_id=None))
    catalog.register_table(new_schema)
    with pytest.raises(MiniSQLError, match="UNKNOWN_TABLE"):
        executor.execute(plan)
    assert catalog.get_table("t") == new_schema
    with pytest.raises(MiniSQLError, match="PROTECTED_TABLE"):
        executor.execute(DropTable(SYSTEM_CATALOG))


@pytest.mark.parametrize("rows", [0, 1, 30])
def test_drop_restart_recreate_and_other_table(tmp_path, rows):
    path = tmp_path / "db"
    db = open_database(path)
    try:
        db.execute("CREATE TABLE t(id INT,name VARCHAR); CREATE TABLE keep(id INT);")
        db.execute("INSERT INTO keep(id) VALUES (99);")
        for i in range(rows):
            db.execute(f"INSERT INTO t(id,name) VALUES ({i},'{('中' * 100)}');")
        old_schema = db.catalog.get_table("t")
        assert db.execute("DROP TABLE T;")[0].message == "表 t 已删除"
        assert db.catalog.get_table("t") is None
        for sql in ("SELECT * FROM t;", "INSERT INTO t(id,name) VALUES (1,'x');", "DROP TABLE t;"):
            with pytest.raises(MiniSQLError, match="UNKNOWN_TABLE"):
                db.execute(sql)
        assert db.execute("SELECT * FROM keep;")[0].rows == ((99,),)
    finally:
        db.close()
    db = open_database(path)
    try:
        assert db.catalog.get_table("t") is None
        assert db.execute("SELECT * FROM keep;")[0].rows == ((99,),)
        results = db.execute("CREATE TABLE t(label VARCHAR); INSERT INTO t(label) VALUES ('new'); SELECT * FROM t;")
        assert results[-1].rows == (("new",),)
        assert db.catalog.get_table("t").table_id != old_schema.table_id
    finally:
        db.close()
    db = open_database(path)
    try:
        assert db.execute("SELECT * FROM t;")[0].rows == (("new",),)
    finally:
        db.close()


@pytest.mark.parametrize("capacity", [1, 64])
@pytest.mark.parametrize("policy", ["LRU", "FIFO"])
def test_drop_reclaims_pages_and_invalidates_dirty_cache(tmp_path, capacity, policy):
    pages = DiskPageManager(FileManager(tmp_path / "heap.db"))
    buffer = PageBufferPool(pages, capacity=capacity, policy=policy)
    storage = HeapStorage(pages, buffer)
    try:
        schema = storage.create_table(TableSchema("old", (ColumnSchema("text", DataType.VARCHAR),)))
        ids = {storage.insert(schema, ("a" * 3000,)).page_id for _ in range(3)}
        before = decode_meta(pages.read_page(0)).next_page_id
        storage.drop_table(schema)  # 包括尚未写回的脏页。
        storage.flush()
        with pytest.raises(MiniSQLError, match="UNKNOWN_TABLE"):
            list(storage.scan(schema))
        with pytest.raises(MiniSQLError, match="UNKNOWN_TABLE"):
            storage.drop_table(schema)
        new_schema = storage.create_table(TableSchema("new", schema.columns))
        new_ids = {storage.insert(new_schema, ("b" * 3000,)).page_id for _ in range(3)}
        assert ids == new_ids
        assert decode_meta(pages.read_page(0)).next_page_id == before
        assert [record.row for record in storage.scan(new_schema)] == [("b" * 3000,)] * 3
        with pytest.raises(MiniSQLError, match="PROTECTED_TABLE"):
            storage.drop_table(SYSTEM_CATALOG)
    finally:
        storage.close()


def test_drop_cli_and_same_batch_recreate(tmp_path, capsys):
    path = tmp_path / "commands.sql"
    path.write_text("CREATE TABLE t(id INT); DROP TABLE t; CREATE TABLE t(name VARCHAR);", encoding="utf-8")
    assert main(["--data-dir", str(tmp_path / "db"), "--file", str(path)]) == 0
    output = capsys.readouterr()
    assert "表 t 已删除" in output.out
    assert output.err == ""


def test_system_catalog_protected_and_error_stops_later_drop(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT);")
        with pytest.raises(MiniSQLError, match="PROTECTED_TABLE"):
            db.execute("DROP TABLE __catalog; DROP TABLE t;")
        assert db.catalog.get_table("t") is not None
        with pytest.raises(MiniSQLError, match="PROTECTED_TABLE"):
            db.catalog.unregister_table("__CATALOG")
    finally:
        db.close()
