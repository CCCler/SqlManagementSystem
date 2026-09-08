"""集成验收：Database 生命周期与多语句执行。

先用 FakeCompiler + MemoryStorage 验证；存储模块落地后，重启持久化用真实页存储验收；
真实编译器场景覆盖 WHERE、投影、错误定位和关闭重启。"""
from pathlib import Path

import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.contracts.models import SourcePosition
from minisql.engine.catalog import PersistentCatalog
from minisql.engine.database import Database, open_database
from minisql.engine.executor import PlanExecutor
from minisql.storage.buffer import PageBufferPool
from minisql.storage.file_manager import FileManager
from minisql.storage.page import DiskPageManager
from minisql.storage.record import HeapStorage
from tests.fakes.compiler import FakeCompiler
from tests.fakes.memory import MemoryCatalog, MemoryStorage


def build_memory_database() -> tuple[Database, MemoryStorage]:
    storage = MemoryStorage()
    catalog = MemoryCatalog()
    catalog.bootstrap()
    return Database(FakeCompiler(), PlanExecutor(storage, catalog), catalog, storage), storage


def build_real_database(path: Path) -> Database:
    """按 open_database 的装配方式用真实页存储构建 Database（编译器仍为替身）。"""
    files = FileManager(path)
    pages = DiskPageManager(files)
    buffer = PageBufferPool(pages)
    storage = HeapStorage(pages, buffer)
    catalog = PersistentCatalog(storage)
    catalog.bootstrap()
    return Database(FakeCompiler(), PlanExecutor(storage, catalog), catalog, storage)


def test_multi_statement_create_then_insert():
    database, storage = build_memory_database()
    results = database.execute(
        "CREATE TABLE student(id INT, name VARCHAR, age INT);\n"
        "INSERT INTO student(id, name, age) VALUES (1, 'Alice', 20);"
    )
    assert len(results) == 2
    assert results[0].message == "表 student 已创建"
    schema = database.catalog.get_table("student")
    assert [record.row for record in storage.scan(schema)] == [(1, "Alice", 20)]


def test_stop_after_error_keeps_prior_effects():
    database, storage = build_memory_database()
    database.execute("CREATE TABLE student(id INT, name VARCHAR);")
    with pytest.raises(MiniSQLError) as error:
        database.execute(
            "INSERT INTO student(id, name) VALUES (1, 'Alice');\n"
            "INSERT INTO missing(id) VALUES (2);"
        )
    assert error.value.code == "UNKNOWN_TABLE"
    schema = database.catalog.get_table("student")
    assert [record.row for record in storage.scan(schema)] == [(1, "Alice")]


def test_open_database_bootstraps_real_storage(tmp_path):
    database = open_database(tmp_path / "db")
    assert database.catalog.list_tables() == ()
    database.close()


def test_restart_data_and_catalog(tmp_path):
    path = tmp_path / "minisql.db"
    database = build_real_database(path)
    database.execute("CREATE TABLE student(id INT, name VARCHAR);")
    database.execute("INSERT INTO student(id, name) VALUES (1, 'Alice');")
    database.close()

    reopened = build_real_database(path)
    schema = reopened.catalog.get_table("student")
    assert [record.row for record in reopened.storage.scan(schema)] == [(1, "Alice")]
    reopened.close()


def test_core_sql_sequence_and_real_restart(tmp_path):
    path = tmp_path / "real_db"
    database = open_database(path)
    try:
        sql = (Path(__file__).resolve().parents[2] / "examples" / "core.sql").read_text(encoding="utf-8")
        results = database.execute(sql)
        assert len(results) == 6
        assert results[3].columns == ("id", "name")
        assert results[3].rows == ((1, "Alice"),)
        assert results[4].affected_rows == 1
        assert results[5].rows == ((2, "Bob"),)
        schema = database.catalog.get_table("student")
    finally:
        database.close()
    reopened = open_database(path)
    try:
        assert reopened.catalog.get_table("STUDENT") == schema
        assert reopened.execute("SELECT * FROM student;")[0].rows == ((2, "Bob", 17),)
    finally:
        reopened.close()


def test_whole_file_error_position_and_prior_effects(tmp_path):
    database = open_database(tmp_path / "db")
    try:
        sql = (
            "CREATE TABLE t(id INT, name VARCHAR);\n"
            "INSERT INTO t(name,id) VALUES ('中'';文',1); -- ;\n"
            "SELECT missing FROM t;\n"
            "DELETE FROM t;"
        )
        with pytest.raises(MiniSQLError) as error:
            database.execute(sql)
        assert error.value.code == "UNKNOWN_COLUMN"
        assert error.value.position == SourcePosition(3, 8)
        assert database.execute("SELECT * FROM t;")[0].rows == ((1, "中';文"),)
    finally:
        database.close()


@pytest.mark.parametrize("operator,expected", [
    ("<", (("a",),)), ("<=", (("a",), ("b",))),
    (">", (("中",),)), (">=", (("b",), ("中",))),
])
def test_string_ordering_query_delete_and_optimization(tmp_path, operator, expected):
    database = open_database(tmp_path / "db")
    try:
        database.execute("CREATE TABLE t(name VARCHAR);")
        for value in ("a", "b", "中"):
            database.execute(f"INSERT INTO t(name) VALUES ('{value}');")
        query = f"SELECT name FROM t WHERE name {operator} 'b';"
        compiled = database.compiler.compile(query, database.catalog)
        assert database.executor.execute(compiled.plan).rows == expected
        assert database.execute(query)[0].rows == expected
        constant = database.compiler.compile(
            f"SELECT name FROM t WHERE 'a' {operator} 'b';", database.catalog)
        assert database.executor.execute(constant.plan) == database.executor.execute(constant.optimized_plan)
        assert database.execute(f"DELETE FROM t WHERE name {operator} 'b';")[0].affected_rows == len(expected)
        assert database.execute("SELECT name FROM t;")[0].rows == tuple(
            row for row in (("a",), ("b",), ("中",)) if row not in expected)
    finally:
        database.close()
