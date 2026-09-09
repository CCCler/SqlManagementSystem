"""EXPLAIN / DISTINCT / LIMIT 及运行时整数溢出的端到端验收。"""
import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.engine.database import open_database


def test_explain_select_renders_plan_without_scanning(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT, name VARCHAR);")
        result = db.execute("EXPLAIN SELECT id, name FROM t WHERE id > 1;")[0]
        assert result.columns == ()
        assert result.rows == ()
        assert "Project(id, name)" in result.message
        assert "Filter((id > 1))" in result.message
        assert "SeqScan(t)" in result.message
    finally:
        db.close()


def test_explain_delete_does_not_modify_data(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT); INSERT INTO t(id) VALUES (1); INSERT INTO t(id) VALUES (2);")
        result = db.execute("EXPLAIN DELETE FROM t WHERE id = 1;")[0]
        assert "Delete(t)" in result.message
        assert db.execute("SELECT id FROM t;")[0].rows == ((1,), (2,))
    finally:
        db.close()


def test_explain_still_runs_semantic_check(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT);")
        with pytest.raises(MiniSQLError) as error:
            db.execute("EXPLAIN SELECT * FROM missing;")
        assert error.value.code == "UNKNOWN_TABLE"
    finally:
        db.close()


def test_distinct_removes_duplicate_rows(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT, name VARCHAR);")
        db.execute("INSERT INTO t(id, name) VALUES (1, 'a');"
                   "INSERT INTO t(id, name) VALUES (1, 'a');"
                   "INSERT INTO t(id, name) VALUES (2, 'b');")
        assert db.execute("SELECT DISTINCT id FROM t;")[0].rows == ((1,), (2,))
        assert db.execute("SELECT DISTINCT id, name FROM t;")[0].rows == ((1, "a"), (2, "b"))
    finally:
        db.close()


def test_limit_truncates_result(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT);")
        for i in range(5):
            db.execute(f"INSERT INTO t(id) VALUES ({i});")
        assert db.execute("SELECT id FROM t LIMIT 2;")[0].rows == ((0,), (1,))
        assert db.execute("SELECT id FROM t LIMIT 0;")[0].rows == ()
        assert len(db.execute("SELECT id FROM t LIMIT 10;")[0].rows) == 5
    finally:
        db.close()


def test_distinct_then_limit(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT);")
        db.execute("INSERT INTO t(id) VALUES (1); INSERT INTO t(id) VALUES (1); INSERT INTO t(id) VALUES (2);")
        assert db.execute("SELECT DISTINCT id FROM t LIMIT 1;")[0].rows == ((1,),)
    finally:
        db.close()


def test_runtime_integer_overflow(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT);")
        db.execute(f"INSERT INTO t(id) VALUES ({2 ** 63 - 1});")
        with pytest.raises(MiniSQLError) as error:
            db.execute("SELECT * FROM t WHERE id + 1 > 0;")
        assert error.value.code == "INTEGER_OUT_OF_RANGE"
    finally:
        db.close()


@pytest.mark.parametrize("sql", [
    "SELECT id FROM t LIMIT -1;",
    "SELECT id FROM t LIMIT;",
    "SELECT DISTINCT FROM t;",
])
def test_invalid_distinct_limit_syntax(tmp_path, sql):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT);")
        with pytest.raises(MiniSQLError):
            db.execute(sql)
    finally:
        db.close()
