"""ORDER BY 端到端验收：排序方向、多列，以及与 WHERE/DISTINCT/LIMIT 组合。"""
from minisql.engine.database import open_database


def test_order_by_ascending_and_descending(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT, name VARCHAR);")
        db.execute("INSERT INTO t(id, name) VALUES (1, 'b');"
                   "INSERT INTO t(id, name) VALUES (2, 'a');"
                   "INSERT INTO t(id, name) VALUES (3, 'c');")
        assert db.execute("SELECT name FROM t ORDER BY name;")[0].rows == (("a",), ("b",), ("c",))
        assert db.execute("SELECT name FROM t ORDER BY name DESC;")[0].rows == (("c",), ("b",), ("a",))
        assert db.execute("SELECT id FROM t ORDER BY id DESC;")[0].rows == ((3,), (2,), (1,))
    finally:
        db.close()


def test_order_by_multiple_columns(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(grp INT, id INT);")
        db.execute("INSERT INTO t(grp, id) VALUES (1, 2);"
                   "INSERT INTO t(grp, id) VALUES (1, 1);"
                   "INSERT INTO t(grp, id) VALUES (2, 3);"
                   "INSERT INTO t(grp, id) VALUES (2, 1);")
        assert db.execute("SELECT grp, id FROM t ORDER BY grp ASC, id DESC;")[0].rows == ((1, 2), (1, 1), (2, 3), (2, 1))
    finally:
        db.close()


def test_order_by_then_limit(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT);")
        for i in (5, 1, 4, 2, 3):
            db.execute(f"INSERT INTO t(id) VALUES ({i});")
        assert db.execute("SELECT id FROM t ORDER BY id LIMIT 3;")[0].rows == ((1,), (2,), (3,))
        assert db.execute("SELECT id FROM t ORDER BY id DESC LIMIT 1;")[0].rows == ((5,),)
    finally:
        db.close()


def test_order_by_with_distinct(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT, name VARCHAR);")
        db.execute("INSERT INTO t(id, name) VALUES (1, 'a');"
                   "INSERT INTO t(id, name) VALUES (2, 'b');"
                   "INSERT INTO t(id, name) VALUES (1, 'a');")
        assert db.execute("SELECT DISTINCT id FROM t ORDER BY id DESC;")[0].rows == ((2,), (1,))
    finally:
        db.close()


def test_order_by_with_where(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT, name VARCHAR);")
        db.execute("INSERT INTO t(id, name) VALUES (1, 'a');"
                   "INSERT INTO t(id, name) VALUES (2, 'b');"
                   "INSERT INTO t(id, name) VALUES (3, 'c');")
        assert db.execute("SELECT name FROM t WHERE id > 1 ORDER BY name DESC;")[0].rows == (("c",), ("b",))
    finally:
        db.close()


def test_order_by_explain_renders(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT, name VARCHAR);")
        result = db.execute("EXPLAIN SELECT id, name FROM t ORDER BY name DESC;")[0]
        assert "ORDER BY name DESC" in result.message
    finally:
        db.close()
