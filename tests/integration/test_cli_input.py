"""交互输入使用真实编译器验证注释、字符串和退出行为。"""
import io

import pytest

from minisql.cli.main import main
from minisql.compiler.compiler import SQLCompiler
from minisql.engine.database import Database, open_database
from minisql.engine.executor import PlanExecutor
from tests.fakes.memory import MemoryCatalog, MemoryStorage


@pytest.fixture
def session(monkeypatch):
    storage = MemoryStorage()
    catalog = MemoryCatalog()
    db = Database(SQLCompiler(), PlanExecutor(storage, catalog), catalog, storage)
    closed = []
    monkeypatch.setattr(storage, "close", lambda: closed.append(True))
    monkeypatch.setattr("minisql.cli.main.open_database", lambda path: db)

    def run(sql):
        monkeypatch.setattr("sys.stdin", io.StringIO(sql))
        code = main([])
        assert closed
        return code
    return db, run


@pytest.mark.parametrize("comment", ["-- 行尾注释;", "/* 注释; */", "-- '未闭合引号", "/* ' ; */ -- ;"])
def test_trailing_comments(session, capsys, comment):
    db, run = session
    assert run(f"CREATE TABLE t(id INT); {comment}\nINSERT INTO t(id) VALUES (1); {comment}\nquit\n") == 0
    assert db.execute("SELECT * FROM t;")[0].rows == ((1,),)
    assert capsys.readouterr().err == ""


def test_multiline_string_blank_lines_and_exit_text(session, capsys):
    db, run = session
    assert run("CREATE TABLE t(name VARCHAR);\nINSERT INTO t(name) VALUES ('first;\n\nexit\nquit\nlast'';line'); -- end\nexit\n") == 0
    assert db.execute("SELECT * FROM t;")[0].rows == (("first;\n\nexit\nquit\nlast';line",),)
    assert capsys.readouterr().err == ""


def test_multiline_comment_and_partial_next_statement(session, capsys):
    db, run = session
    assert run("CREATE TABLE t(id INT); /* comment;\nexit\n*/ INSERT INTO t(id)\nVALUES (7); SELECT *\nFROM t; -- tail\nquit\n") == 0
    assert db.execute("SELECT * FROM t;")[0].rows == ((7,),)
    output = capsys.readouterr()
    assert "7" in output.out
    assert output.err == ""


@pytest.mark.parametrize("exit_line", ["", "exit\n", "quit\n"])
def test_incomplete_statement_not_executed_on_exit(session, capsys, exit_line):
    db, run = session
    assert run("CREATE TABLE t(id INT);\nINSERT INTO t(id) VALUES (1)\n" + exit_line) == 1
    assert db.execute("SELECT * FROM t;")[0].rows == ()
    assert "缺少结束分号" in capsys.readouterr().err


@pytest.mark.parametrize("tail,reason", [
    ("INSERT INTO t(name) VALUES ('abc;\n", "字符串未闭合"),
    ("/* abc;\n", "块注释未闭合"),
    ("INSERT INTO t(name) VALUES ('abc')\n", "缺少结束分号"),
])
def test_eof_diagnoses_incomplete_input(session, capsys, tail, reason):
    db, run = session
    assert run("CREATE TABLE t(name VARCHAR); " + tail) == 1
    assert db.catalog.get_table("t") is not None  # 完整前缀已执行。
    assert db.execute("SELECT * FROM t;")[0].rows == ()
    assert reason in capsys.readouterr().err


@pytest.mark.parametrize("sql", ["\n-- comment;\n", "/* comment; */\nexit\n", "-- hi\n/* a\nb */\n"])
def test_comments_only_exit_cleanly(session, capsys, sql):
    _, run = session
    assert run(sql) == 0
    assert capsys.readouterr().err == ""


def test_error_stops_batch_but_next_input_works(session, capsys):
    db, run = session
    assert run("CREATE TABLE t(id INT);\nSELECT missing FROM t; INSERT INTO t(id) VALUES (1); INSERT INTO t(id)\nINSERT INTO t(id) VALUES (2);\nexit\n") == 0
    assert db.execute("SELECT * FROM t;")[0].rows == ((2,),)
    assert "UNKNOWN_COLUMN" in capsys.readouterr().err


def test_continuation_prompt_cancel_and_resume(session, monkeypatch, capsys):
    db, _ = session
    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr("sys.stdin", stream)
    inputs = iter(["CREATE TABLE abandoned(id INT)", KeyboardInterrupt(), "CREATE TABLE t(id INT);", "exit"])
    prompts = []

    def read(prompt):
        prompts.append(prompt)
        item = next(inputs)
        if isinstance(item, BaseException):
            raise item
        return item
    monkeypatch.setattr("builtins.input", read)
    assert main([]) == 0
    assert prompts == ["minisql> ", "   ...> ", "minisql> ", "minisql> "]
    assert db.catalog.get_table("abandoned") is None
    assert db.catalog.get_table("t") is not None
    assert "已取消" in capsys.readouterr().err


def test_complete_prefix_executes_before_reading_more(session, monkeypatch):
    db, _ = session
    monkeypatch.setattr("sys.stdin", io.StringIO())
    count = 0

    def read(prompt):
        nonlocal count
        count += 1
        if count == 1:
            return "CREATE TABLE t(id INT); INSERT INTO t(id)"
        assert db.catalog.get_table("t") is not None
        if count == 2:
            return "VALUES (3); -- tail"
        return "exit"
    monkeypatch.setattr("builtins.input", read)
    assert main([]) == 0
    assert db.execute("SELECT * FROM t;")[0].rows == ((3,),)


def test_eof_keeps_completed_writes_after_real_restart(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db"
    monkeypatch.setattr("sys.stdin", io.StringIO(
        "CREATE TABLE t(id INT); -- create\n"
        "INSERT INTO t(id) VALUES (1); INSERT INTO t(id) VALUES (2)\n"))
    assert main(["--data-dir", str(path)]) == 1
    assert "未执行" in capsys.readouterr().err
    reopened = open_database(path)
    try:
        assert reopened.execute("SELECT * FROM t;")[0].rows == ((1,),)
    finally:
        reopened.close()


@pytest.mark.parametrize("initial,exception,expected", [
    (None, KeyboardInterrupt, 0),
    ("CREATE TABLE t(id INT)", OSError, 1),
])
def test_idle_interrupt_and_unreadable_input(session, monkeypatch, capsys, initial, exception, expected):
    db, _ = session
    monkeypatch.setattr("sys.stdin", io.StringIO())
    lines = [initial] if initial is not None else []

    def read(prompt):
        if lines:
            return lines.pop()
        raise exception()
    monkeypatch.setattr("builtins.input", read)
    assert main([]) == expected
    assert db.catalog.list_tables() == ()
    assert ("未执行" in capsys.readouterr().err) == (expected == 1)
