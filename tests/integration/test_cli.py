"""CLI 验收：用 FakeCompiler + MemoryStorage 验证文件模式、交互模式和结果渲染。"""
import io
import sys

from minisql.cli.main import main, render_result
from minisql.contracts.models import ExecutionResult
from minisql.engine.database import Database
from minisql.engine.executor import PlanExecutor
from tests.fakes.compiler import FakeCompiler
from tests.fakes.memory import MemoryCatalog, MemoryStorage


def make_database() -> Database:
    storage = MemoryStorage()
    catalog = MemoryCatalog()
    catalog.bootstrap()
    return Database(FakeCompiler(), PlanExecutor(storage, catalog), catalog, storage)


def test_render_query_table():
    result = ExecutionResult(columns=("id", "name"), rows=((1, "Alice"), (2, "Bob")))
    rendered = render_result(result)
    assert "id | name" in rendered
    assert "1  | Alice" in rendered
    assert "2  | Bob" in rendered


def test_render_write_message():
    assert render_result(ExecutionResult(affected_rows=1, message="已插入 1 行")) == "已插入 1 行"
    assert "1 行受影响" in render_result(ExecutionResult(affected_rows=1))


def test_main_file_mode(monkeypatch, capsys, tmp_path):
    sql_file = tmp_path / "demo.sql"
    sql_file.write_text(
        "CREATE TABLE student(id INT, name VARCHAR);\n"
        "INSERT INTO student(id, name) VALUES (1, 'Alice');\n"
        "SELECT * FROM student;",
        encoding="utf-8",
    )
    monkeypatch.setattr("minisql.cli.main.open_database", lambda data_dir: make_database())
    assert main(["--file", str(sql_file)]) == 0
    output = capsys.readouterr().out
    assert "已创建" in output
    assert "Alice" in output


def test_main_file_mode_reports_error(monkeypatch, capsys, tmp_path):
    sql_file = tmp_path / "bad.sql"
    sql_file.write_text("INSERT INTO missing(id) VALUES (1);", encoding="utf-8")
    monkeypatch.setattr("minisql.cli.main.open_database", lambda data_dir: make_database())
    assert main(["--file", str(sql_file)]) == 1
    assert "UNKNOWN_TABLE" in capsys.readouterr().err


def test_main_file_mode_read_error(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("minisql.cli.main.open_database", lambda data_dir: make_database())
    assert main(["--file", str(tmp_path / "missing.sql")]) == 1
    assert "无法读取 SQL 文件" in capsys.readouterr().err


def test_main_interactive_mode(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            "CREATE TABLE student(id INT, name VARCHAR);\n"
            "INSERT INTO student(id, name) VALUES (1, 'Alice');\n"
            "SELECT * FROM student;\n"
            "exit\n"
        ),
    )
    monkeypatch.setattr("minisql.cli.main.open_database", lambda data_dir: make_database())
    assert main([]) == 0
    output = capsys.readouterr().out
    assert "已创建" in output
    assert "Alice" in output


def test_main_interactive_eof_exits_cleanly(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("minisql.cli.main.open_database", lambda data_dir: make_database())
    assert main([]) == 0
