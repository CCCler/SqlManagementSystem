"""异常 I/O 与资源释放边界验收。

覆盖：打开路径不可用、初始化失败后锁释放、写盘故障回滚与连接存活、
CLI 文件模式 I/O 错误后的资源释放、提交标记写入不确定、关闭后文件句柄释放、
以及基础 Database 关闭时刷新/关闭错误不吞掉。"""
import io
import sys

import pytest

from minisql.cli.main import main
from minisql.compiler.compiler import SQLCompiler
from minisql.contracts.errors import MiniSQLError
from minisql.engine.database import Database, open_database
from minisql.engine.executor import PlanExecutor
from minisql.storage.file_manager import FileManager
from minisql.storage.journal import RollbackJournal
from tests.fakes.memory import MemoryCatalog, MemoryStorage


def test_open_database_rejects_file_path(tmp_path):
    occupied = tmp_path / "occupied"
    occupied.write_text("x", encoding="utf-8")
    with pytest.raises(OSError):
        open_database(occupied)


def test_cli_reports_unopenable_data_dir(tmp_path, capsys):
    occupied = tmp_path / "occupied"
    occupied.write_text("x", encoding="utf-8")
    assert main(["--data-dir", str(occupied)]) == 1
    assert "无法打开数据库目录" in capsys.readouterr().err


def test_failed_open_releases_lock(monkeypatch, tmp_path):
    def fail_begin(journal):
        raise OSError("模拟日志初始化失败")
    with monkeypatch.context() as patch:
        patch.setattr(RollbackJournal, "begin", fail_begin)
        with pytest.raises(OSError):
            open_database(tmp_path)
    database = open_database(tmp_path)
    database.execute("CREATE TABLE t(id INT);")
    database.close()


def test_io_error_rolls_back_and_connection_survives(monkeypatch, tmp_path):
    database = open_database(tmp_path)
    try:
        database.execute("CREATE TABLE t(id INT);")
        calls = 0
        original = FileManager.write_at

        def failing(files, offset, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("模拟写入失败")
            return original(files, offset, data)

        with monkeypatch.context() as patch:
            patch.setattr(FileManager, "write_at", failing)
            with pytest.raises(MiniSQLError) as error:
                database.execute("INSERT INTO t(id) VALUES (1);")
            assert error.value.code == "IO_ERROR"
            assert error.value.stage.value == "storage"
        assert calls == 1
        database.execute("INSERT INTO t(id) VALUES (2);")
        assert database.execute("SELECT * FROM t;")[0].rows == ((2,),)
    finally:
        database.close()


def test_cli_file_mode_io_error_releases_lock(monkeypatch, tmp_path, capsys):
    sql_file = tmp_path / "demo.sql"
    sql_file.write_text("CREATE TABLE t(id INT);", encoding="utf-8")
    data_dir = tmp_path / "db"
    original = FileManager.write_at

    def failing(files, offset, data):
        raise OSError("模拟写入失败")

    with monkeypatch.context() as patch:
        patch.setattr(FileManager, "write_at", failing)
        # 写故障命中打开阶段的 bootstrap，CLI 按无法打开数据库目录报告。
        assert main(["--data-dir", str(data_dir), "--file", str(sql_file)]) == 1
        assert "无法打开数据库目录" in capsys.readouterr().err
    # 失败打开的构造函数清理必须释放锁，随后可以正常重开。
    database = open_database(data_dir)
    try:
        assert database.catalog.list_tables() == ()
    finally:
        database.close()


def test_commit_uncertain_marks_connection_broken(monkeypatch, tmp_path):
    def fail_commit(journal):
        raise OSError("模拟提交标记写入失败")

    database = open_database(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(RollbackJournal, "commit", fail_commit)
        with pytest.raises(MiniSQLError) as error:
            database.execute("CREATE TABLE t(id INT);")
        assert error.value.code == "COMMIT_UNCERTAIN"
    with pytest.raises(MiniSQLError) as error:
        database.execute("SELECT * FROM t;")
    assert error.value.code == "CONNECTION_CLOSED"
    database.close()

    reopened = open_database(tmp_path)
    try:
        assert reopened.catalog.list_tables() == ()
    finally:
        reopened.close()


def test_close_releases_file_handles(tmp_path):
    database = open_database(tmp_path)
    database.execute("CREATE TABLE t(id INT); INSERT INTO t(id) VALUES (1);")
    database.close()
    # Windows 下句柄未释放时 unlink 会抛 PermissionError。
    (tmp_path / "minisql.db").unlink()
    (tmp_path / "minisql.lock").unlink()
    assert not (tmp_path / "minisql.journal").exists()


def test_base_database_close_propagates_flush_error():
    class FailingStorage(MemoryStorage):
        def close(self):
            raise OSError("模拟关闭失败")

    storage = FailingStorage()
    catalog = MemoryCatalog()
    database = Database(SQLCompiler(), PlanExecutor(storage, catalog), catalog, storage)
    with pytest.raises(OSError):
        database.close()
