"""真实 GUI 会话、HTTP 边界与持久化测试，数据库均使用隔离目录。"""
import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from minisql.gui.server import WorkbenchServer, MAX_BODY
from minisql.gui.session import Session


@pytest.fixture
def session(tmp_path):
    connection = Session(tmp_path / "database")
    assert connection.submit("connect", {}).result()["ok"]
    yield connection
    connection.close()


def run(session, sql):
    return session.submit("execute", {"sql": sql}).result()


def test_gui_core_persistence_and_trace(session):
    data = run(session, "CREATE TABLE t(id INT, name VARCHAR); INSERT INTO t(id,name) VALUES (1,'中文'); SELECT * FROM t;")
    assert data["ok"]
    assert data["results"][-1]["rows"] == [[1, "中文"]]
    assert len(data["compilations"]) == 3
    assert data["compilations"][-1]["tokens"][0]["lexeme"] == "SELECT"
    assert data["compilations"][-1]["ast"]["node"] == "SelectStmt"
    assert data["cache"]["hits"] >= 0 and data["cache"]["misses"] > 0
    assert data["tables"][0]["columns"][1]["name"] == "name"
    session.submit("disconnect", {}).result()
    session.submit("connect", {}).result()
    assert run(session, "SELECT * FROM t;")["results"][0]["rows"] == [[1, "中文"]]
    assert run(session, "DELETE FROM t WHERE id=1;")["results"][0]["affected_rows"] == 1
    assert run(session, "SELECT * FROM t;")["results"][0]["rows"] == []
    assert run(session, "DROP TABLE t;")["tables"] == []


def test_gui_failure_preserves_batch_contract(session):
    data = run(session, "CREATE TABLE t(id INT);\nINSERT INTO t(id) VALUES (1);\nSELECT missing FROM t;\nINSERT INTO t(id) VALUES (2);")
    assert not data["ok"] and data["results"] == []
    assert data["error"]["position"]["line"] == 3
    assert len(data["compilations"]) == 2
    assert run(session, "SELECT * FROM t;")["results"][0]["rows"] == [[1]]


def test_gui_explicit_transaction_and_failed_state(session):
    run(session, "CREATE TABLE t(id INT);")
    assert run(session, "BEGIN;")["in_transaction"]
    run(session, "INSERT INTO t(id) VALUES (1);")
    data = run(session, "INSERT INTO t(id) VALUES ('bad');")
    assert not data["ok"] and data["transaction_failed"]
    assert run(session, "COMMIT;")["error"]["code"] == "TRANSACTION_ABORTED"
    data = run(session, "ROLLBACK;")
    assert data["ok"] and not data["in_transaction"] and not data["transaction_failed"]
    assert run(session, "SELECT * FROM t;")["results"][0]["rows"] == []
    assert run(session, "BEGIN; INSERT INTO t(id) VALUES (2); COMMIT;")["ok"]
    assert run(session, "SELECT * FROM t;")["results"][0]["rows"] == [[2]]


def test_disconnect_rolls_back_and_reconnects(session):
    run(session, "CREATE TABLE t(id INT); BEGIN; INSERT INTO t(id) VALUES (1);")
    data = session.submit("disconnect", {}).result()
    assert data["ok"] and not data["connected"]
    session.submit("connect", {}).result()
    assert run(session, "SELECT * FROM t;")["results"][0]["rows"] == []


def test_switch_directory_and_guard_transaction(session, tmp_path):
    run(session, "CREATE TABLE first(id INT); BEGIN;")
    with pytest.raises(ValueError, match="提交或回滚"):
        session.submit("connect", {"directory": str(tmp_path / "second")}).result()
    assert session.db.in_transaction
    run(session, "ROLLBACK;")
    data = session.submit("connect", {"directory": str(tmp_path / "second")}).result()
    assert data["ok"] and data["tables"] == []
    assert Path(data["directory"]).name == "second"


def test_separate_sessions_lock_and_catalog_refresh(session):
    other = Session(session.path)
    try:
        assert other.submit("connect", {}).result()["ok"]
        run(session, "CREATE TABLE t(id INT);")
        assert other.submit("state", {}).result()["tables"][0]["name"] == "t"
        run(session, "BEGIN; INSERT INTO t(id) VALUES (1);")
        data = run(other, "SELECT * FROM t;")
        assert not data["ok"] and data["error"]["code"] == "DATABASE_BUSY"
        run(session, "COMMIT;")
        assert run(other, "SELECT * FROM t;")["results"][0]["rows"] == [[1]]
    finally:
        other.close()


def test_close_releases_transaction(session):
    run(session, "CREATE TABLE t(id INT); BEGIN; INSERT INTO t(id) VALUES (1);")
    session.close()
    other = Session(session.path)
    try:
        other.submit("connect", {}).result()
        assert run(other, "SELECT * FROM t;")["results"][0]["rows"] == []
    finally:
        other.close()


def test_display_capture_does_not_execute_twice(session):
    run(session, "CREATE TABLE t(id INT);")
    data = run(session, "INSERT INTO t(id) VALUES (1);")
    assert len(data["compilations"]) == 1
    assert data["cache"]["writebacks"] > 0
    assert run(session, "SELECT * FROM t;")["results"][0]["rows"] == [[1]]


def test_invalid_sql_and_empty_comments(session):
    with pytest.raises(ValueError):
        run(session, " ")
    data = run(session, "-- comment")
    assert data["ok"] and data["results"] == []
    data = run(session, "SELECT '")
    assert not data["ok"] and data["error"]["stage"] == "lexical"


@pytest.fixture
def server(tmp_path):
    service = WorkbenchServer(("127.0.0.1", 0), tmp_path / "http-database")
    thread = threading.Thread(target=service.serve_forever)
    thread.start()
    yield service
    service.shutdown()
    thread.join()
    service.server_close()


def request(server, route, body=None, headers=None):
    values = {"X-MiniSQL-Token": server.token, "Origin": server.origin}
    if headers:
        values.update(headers)
    req = Request(server.origin + route, data=json.dumps(body).encode() if body is not None else None,
                  headers=values)
    try:
        response = urlopen(req, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read()
        return response.status, json.loads(raw) if "application/json" in response.headers["Content-Type"] else raw


def test_http_real_connection_execute_disconnect(server):
    code, body = request(server, "/api/session", {})
    assert code == 200
    key = {"session": body["session"]}
    assert request(server, "/api/connect", key)[1]["connected"]
    data = request(server, "/api/execute", {**key, "sql": "CREATE TABLE t(id INT);"})[1]
    assert data["ok"] and data["tables"][0]["name"] == "t"
    assert not request(server, "/api/disconnect", key)[1]["connected"]
    assert request(server, "/api/close", key)[0] == 200
    assert request(server, "/api/state", key)[0] == 410


@pytest.mark.parametrize("headers", [{"X-MiniSQL-Token": "invalid"}, {"Origin": "https://example.com"}, {"Host": "example.com"}])
def test_http_rejects_foreign_requests(server, headers):
    assert request(server, "/api/session", {}, headers)[0] == 403


def test_http_assets_and_invalid_requests(server):
    for name in ["/", "/app.js", "/style.css", "/favicon.svg"]:
        assert request(server, name)[0] == 200
    assert request(server, "/../session.py")[0] == 404
    assert request(server, "/api/session", [])[0] == 400
    assert request(server, "/api/execute", {"session": "unknown"})[0] == 410
    assert request(server, "/api/nope", {})[0] == 404
    assert request(server, "/api/session", {"sql": "x" * MAX_BODY})[0] == 400


def test_idle_cleanup_rolls_back(server):
    key = request(server, "/api/session", {})[1]["session"]
    request(server, "/api/connect", {"session": key})
    request(server, "/api/execute", {"session": key, "sql": "CREATE TABLE t(id INT); BEGIN; INSERT INTO t(id) VALUES (1);"})
    session = server.sessions[key]
    with session.guard:
        session.last_used = time.monotonic() - 2000
    server.service_actions()
    assert key not in server.sessions
    other = Session(server.path)
    try:
        other.submit("connect", {}).result()
        assert run(other, "SELECT * FROM t;")["results"][0]["rows"] == []
    finally:
        other.close()
