"""真实数据库上的事务、故障注入及连接互斥。"""
from concurrent.futures import ThreadPoolExecutor
import io
import threading

import pytest

from minisql.cli.main import main
from minisql.compiler.compiler import SQLCompiler
from minisql.contracts.ast import TransactionStmt
from minisql.contracts.errors import MiniSQLError
from minisql.contracts.plans import TransactionControl
from minisql.engine.database import open_database
from tests.fakes.memory import MemoryCatalog


@pytest.mark.parametrize("action", ["BEGIN", "COMMIT", "ROLLBACK"])
def test_transaction_compilation(action):
    compiled = SQLCompiler().compile(f"{action.lower()};", MemoryCatalog())
    assert isinstance(compiled.ast, TransactionStmt)
    assert compiled.semantic.schema is None
    assert compiled.plan == compiled.optimized_plan == TransactionControl(action)


@pytest.mark.parametrize("sql", ["BEGIN", "BEGIN t;", "COMMIT t;", "ROLLBACK t;"])
def test_transaction_syntax(sql):
    with pytest.raises(MiniSQLError, match="UNEXPECTED_TOKEN"):
        SQLCompiler().compile(sql, MemoryCatalog())


def test_explicit_commit_and_rollback_include_ddl_and_reclaim(tmp_path):
    db = open_database(tmp_path)
    try:
        db.execute("CREATE TABLE t(id INT); INSERT INTO t(id) VALUES (1);")
        db.execute("BEGIN; DELETE FROM t; INSERT INTO t(id) VALUES (2); CREATE TABLE extra(id INT);")
        assert db.execute("SELECT * FROM t;")[0].rows == ((2,),)
        db.rollback()
        assert db.execute("SELECT * FROM t;")[0].rows == ((1,),)
        assert db.catalog.get_table("extra") is None
        db.execute("BEGIN; DROP TABLE t; CREATE TABLE t(name VARCHAR); INSERT INTO t(name) VALUES ('new'); ROLLBACK;")
        assert db.execute("SELECT * FROM t;")[0].rows == ((1,),)
        db.execute("BEGIN; INSERT INTO t(id) VALUES (3); COMMIT;")
    finally:
        db.close()
    db = open_database(tmp_path)
    try:
        assert db.execute("SELECT * FROM t;")[0].rows == ((1,), (3,))
    finally:
        db.close()


def test_error_aborts_explicit_transaction_until_rollback(tmp_path):
    db = open_database(tmp_path)
    try:
        db.execute("CREATE TABLE t(id INT); BEGIN; INSERT INTO t(id) VALUES (1);")
        with pytest.raises(MiniSQLError, match="UNKNOWN_TABLE"):
            db.execute("INSERT INTO missing(id) VALUES (2); COMMIT;")
        for sql in ("SELECT * FROM t;", "COMMIT;", "BEGIN;"):
            with pytest.raises(MiniSQLError, match="TRANSACTION_ABORTED"):
                db.execute(sql)
        db.rollback()
        assert db.execute("SELECT * FROM t;")[0].rows == ()
        db.execute("INSERT INTO t(id) VALUES (4);")
    finally:
        db.close()


@pytest.mark.parametrize("action", ["COMMIT", "ROLLBACK"])
def test_control_without_begin(tmp_path, action):
    db = open_database(tmp_path)
    try:
        with pytest.raises(MiniSQLError, match="NO_TRANSACTION"):
            db.execute(action + ";")
    finally:
        db.close()


def test_nested_begin_and_close_rollback(tmp_path):
    db = open_database(tmp_path)
    db.execute("BEGIN; CREATE TABLE pending(id INT);")
    with pytest.raises(MiniSQLError, match="TRANSACTION_ACTIVE"):
        db.begin()
    db.close()
    db.close()
    with pytest.raises(MiniSQLError, match="CONNECTION_CLOSED"):
        db.execute("SELECT * FROM pending;")
    db = open_database(tmp_path)
    try:
        assert db.catalog.list_tables() == ()
    finally:
        db.close()


def test_autocommit_keeps_prior_success_and_undoes_partial_statement(tmp_path, monkeypatch):
    db = open_database(tmp_path)
    db.execute("CREATE TABLE t(id INT); INSERT INTO t(id) VALUES (1);")
    from minisql.engine.executor import PlanExecutor
    original = PlanExecutor._insert

    def failing_insert(executor, plan):
        original(executor, plan)
        raise OSError("模拟写入后故障")
    with monkeypatch.context() as patch:
        patch.setattr(PlanExecutor, "_insert", failing_insert)
        with pytest.raises(MiniSQLError, match="IO_ERROR"):
            db.execute("INSERT INTO t(id) VALUES (2);")
    assert db.execute("SELECT * FROM t;")[0].rows == ((1,),)
    db.close()


def test_two_connections_serialize_and_refresh_cache(tmp_path):
    first = open_database(tmp_path, lock_timeout=0.03)
    second = open_database(tmp_path, lock_timeout=0.03)
    try:
        first.execute("CREATE TABLE t(id INT);")
        assert second.execute("SELECT * FROM t;")[0].rows == ()
        first.execute("BEGIN; INSERT INTO t(id) VALUES (1);")
        with pytest.raises(MiniSQLError, match="DATABASE_BUSY"):
            second.execute("SELECT * FROM t;")
        with pytest.raises(MiniSQLError, match="DATABASE_BUSY"):
            second.execute("INSERT INTO t(id) VALUES (2);")
        first.commit()
        assert second.execute("SELECT * FROM t;")[0].rows == ((1,),)
        second.execute("INSERT INTO t(id) VALUES (2);")
        assert first.execute("SELECT * FROM t;")[0].rows == ((1,), (2,))
    finally:
        first.close()
        second.close()


def test_separate_connections_threaded_updates_no_lost_rows(tmp_path):
    db = open_database(tmp_path)
    db.execute("CREATE TABLE t(id INT);")
    db.close()
    barrier = threading.Barrier(3)

    def writer(number):
        connection = open_database(tmp_path)
        try:
            barrier.wait(timeout=5)
            for i in range(8):
                connection.execute(f"INSERT INTO t(id) VALUES ({number * 10 + i});")
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(writer, range(3)))
    db = open_database(tmp_path)
    try:
        assert sorted(db.execute("SELECT * FROM t;")[0].rows) == [(n * 10 + i,) for n in range(3) for i in range(8)]
    finally:
        db.close()


def test_connection_explicit_transaction_thread_owner(tmp_path):
    db = open_database(tmp_path)
    try:
        db.begin()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(db.execute, "CREATE TABLE wrong(id INT);")
            with pytest.raises(MiniSQLError, match="TRANSACTION_OWNER"):
                future.result()
        db.execute("CREATE TABLE correct(id INT);")
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("ending", ["", "exit\n", "quit\n"])
def test_cli_uncommitted_transaction_reports_rollback(tmp_path, monkeypatch, capsys, ending):
    monkeypatch.setattr("sys.stdin", io.StringIO("BEGIN; CREATE TABLE t(id INT);\n" + ending))
    assert main(["--data-dir", str(tmp_path)]) == 1
    assert "未提交事务，已回滚" in capsys.readouterr().err
    db = open_database(tmp_path)
    try:
        assert db.catalog.list_tables() == ()
    finally:
        db.close()


def test_cli_transaction_file_commits(tmp_path, capsys):
    path = tmp_path / "run.sql"
    path.write_text("BEGIN; CREATE TABLE t(id INT); INSERT INTO t(id) VALUES (7); COMMIT;", encoding="utf-8")
    assert main(["--data-dir", str(tmp_path / "db"), "--file", str(path), "--lock-timeout", "0.1"]) == 0
    assert "事务已提交" in capsys.readouterr().out


def test_data_sync_failure_rolls_back_autocommit(tmp_path, monkeypatch):
    from minisql.storage.file_manager import FileManager
    db = open_database(tmp_path)
    db.execute("CREATE TABLE t(id INT);")
    def fail(files):
        raise OSError("模拟数据同步失败")
    with monkeypatch.context() as patch:
        patch.setattr(FileManager, "sync", fail)
        with pytest.raises(MiniSQLError, match="IO_ERROR"):
            db.execute("INSERT INTO t(id) VALUES (1);")
    assert db.execute("SELECT * FROM t;")[0].rows == ()
    db.close()


@pytest.mark.parametrize("timeout", ["-1", "nan", "inf"])
def test_cli_rejects_invalid_timeout(timeout):
    with pytest.raises(SystemExit) as error:
        main(["--lock-timeout", timeout])
    assert error.value.code == 2
