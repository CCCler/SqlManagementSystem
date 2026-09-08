"""集成验收：Database 生命周期与多语句执行。

先用 FakeCompiler + MemoryStorage 验证；存储模块落地后，重启持久化用真实页存储验收；
依赖真实编译器（WHERE/投影/错误定位）的场景在联调（阶段 2/3）后启用。"""
from pathlib import Path

import pytest

from minisql.contracts.errors import MiniSQLError
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


@pytest.mark.skip(reason="等待成员一真实编译器完成后启用（联调阶段 2/3）")
@pytest.mark.parametrize("scenario", ["core_sql_sequence", "whole_file_error_position"])
def test_deferred_integration(scenario):
    pytest.fail(f"联调后编写真实验收操作与断言: {scenario}")
