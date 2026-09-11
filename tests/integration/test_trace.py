import io
import json

import pytest

from minisql.cli.main import main
from minisql.engine.database import open_database


def events(output):
    return [json.loads(line) for line in output.splitlines()]


def test_file_trace_shows_pipeline_and_real_execution(tmp_path, capsys):
    script = tmp_path / "demo.sql"
    script.write_text("CREATE TABLE t(id INT, name VARCHAR);\n"
                      "INSERT INTO t(name,id) VALUES ('中文',20);\n"
                      "SELECT name FROM t WHERE 1=1 AND id>10+8;", encoding="utf-8")
    path = tmp_path / "db"
    assert main(["--data-dir", str(path), "--file", str(script), "--trace"]) == 0
    captured = capsys.readouterr()
    assert not captured.err
    records = events(captured.out)
    compiled = [r for r in records if r["event"] == "compilation"]
    assert len(compiled) == 3
    query = compiled[-1]
    assert list(query) == ["event", "tokens", "ast", "semantic", "plan", "optimized_plan", "output_fields", "dependencies", "required_capabilities"]
    assert query["tokens"][0]["position"] == {"node": "SourcePosition", "line": 3, "column": 1}
    assert query["ast"]["node"] == "SelectStmt"
    assert query["semantic"]["schema"]["name"] == "t"
    assert query["plan"]["source"]["predicate"]["operator"] == "AND"
    optimized = query["optimized_plan"]["source"]["predicate"]
    assert optimized["operator"] == ">" and optimized["right"]["value"] == 18
    assert records[-1]["result"]["rows"] == [["中文"]]
    assert "中文" in captured.out
    db = open_database(path)
    try:
        assert db.execute("SELECT id FROM t;")[0].rows == ((20,),)
    finally:
        db.close()


def test_trace_transaction_and_interactive_recovery(tmp_path, monkeypatch, capsys):
    deep = "NOT " * 1200 + "TRUE"
    monkeypatch.setattr("sys.stdin", io.StringIO(
        "CREATE TABLE t(id INT);\nBEGIN;\nINSERT INTO t(id) VALUES (1);\n"
        f"SELECT * FROM t WHERE {deep};\nROLLBACK;\nSELECT * FROM t;\nexit\n"))
    assert main(["--data-dir", str(tmp_path / "db"), "--trace"]) == 0
    captured = capsys.readouterr()
    assert "EXPRESSION_TOO_COMPLEX" in captured.err
    assert "Traceback" not in captured.err
    records = events(captured.out)
    controls = [r for r in records if r["event"] == "compilation" and r["ast"]["node"] == "TransactionStmt"]
    assert [r["plan"]["action"] for r in controls] == ["BEGIN", "ROLLBACK"]
    assert all(r["semantic"]["schema"] is None for r in controls)
    assert records[-1]["result"]["rows"] == []


@pytest.mark.parametrize("trace", [False, True])
def test_deep_file_error_is_clean_and_connection_reopens(tmp_path, capsys, trace):
    path = tmp_path / "db"
    script = tmp_path / "bad.sql"
    script.write_text("CREATE TABLE t(id INT);\nSELECT * FROM t WHERE " + "(" * 300 + "TRUE;", encoding="utf-8")
    assert main(["--data-dir", str(path), "--file", str(script)] + (["--trace"] if trace else [])) == 1
    captured = capsys.readouterr()
    assert "EXPRESSION_TOO_COMPLEX at 2:" in captured.err
    assert "Traceback" not in captured.err
    db = open_database(path)
    try:
        assert db.execute("SELECT * FROM t;")[0].rows == ()
    finally:
        db.close()


def test_trace_at_complexity_limit_serializes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(
        "CREATE TABLE t(id INT);\nSELECT * FROM t WHERE " + " AND ".join(["TRUE"] * 65) + ";\n"))
    assert main(["--data-dir", str(tmp_path / "db"), "--trace"]) == 0
    assert len(events(capsys.readouterr().out)) == 4


def test_trace_interrupt_keeps_stdout_valid_json_lines(tmp_path, monkeypatch, capsys):
    inputs = iter(["CREATE TABLE t(id INT);", KeyboardInterrupt()])
    def read(prompt):
        value = next(inputs)
        if isinstance(value, BaseException):
            raise value
        return value
    monkeypatch.setattr("builtins.input", read)
    assert main(["--data-dir", str(tmp_path / "db"), "--trace"]) == 0
    assert len(events(capsys.readouterr().out)) == 2
