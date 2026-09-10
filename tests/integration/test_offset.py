"""LIMIT/OFFSET 端到端验收：分页、与 WHERE/ORDER BY/DISTINCT 组合。"""
from minisql.engine.database import open_database


def test_offset_pagination(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT);")
        for i in range(10):
            db.execute(f"INSERT INTO t(id) VALUES ({i});")
        assert db.execute("SELECT id FROM t ORDER BY id LIMIT 3 OFFSET 0;")[0].rows == ((0,), (1,), (2,))
        assert db.execute("SELECT id FROM t ORDER BY id LIMIT 3 OFFSET 3;")[0].rows == ((3,), (4,), (5,))
        assert db.execute("SELECT id FROM t ORDER BY id LIMIT 3 OFFSET 8;")[0].rows == ((8,), (9,))
        assert db.execute("SELECT id FROM t ORDER BY id LIMIT 3 OFFSET 100;")[0].rows == ()
    finally:
        db.close()


def test_offset_with_where_and_order_by(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT, name VARCHAR);")
        db.execute("INSERT INTO t(id, name) VALUES (1, 'a');"
                   "INSERT INTO t(id, name) VALUES (2, 'b');"
                   "INSERT INTO t(id, name) VALUES (3, 'c');"
                   "INSERT INTO t(id, name) VALUES (4, 'd');")
        assert db.execute("SELECT id, name FROM t WHERE id > 1 ORDER BY id DESC LIMIT 2 OFFSET 1;")[0].rows == ((3, "c"), (2, "b"))
    finally:
        db.close()


def test_offset_with_distinct(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT);")
        db.execute("INSERT INTO t(id) VALUES (1); INSERT INTO t(id) VALUES (1); INSERT INTO t(id) VALUES (2); INSERT INTO t(id) VALUES (3);")
        assert db.execute("SELECT DISTINCT id FROM t ORDER BY id LIMIT 2 OFFSET 1;")[0].rows == ((2,), (3,))
    finally:
        db.close()


def test_offset_explain_renders(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT);")
        result = db.execute("EXPLAIN SELECT id FROM t LIMIT 5 OFFSET 2;")[0]
        assert "LIMIT 5" in result.message
        assert "OFFSET 2" in result.message
    finally:
        db.close()
