"""最终验收：真实 SQL 的跨模块组合及扫描/索引结果一致性。"""
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
import os

import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.engine.database import open_database


@pytest.fixture
def db(tmp_path):
    database = open_database(tmp_path / "db")
    yield database
    database.close()


def rows(db, sql):
    return db.execute(sql)[0].rows


def test_index_survives_non_key_update_and_reopen(db, tmp_path):
    db.execute("CREATE TABLE t(id INT, note VARCHAR); INSERT INTO t(id,note) VALUES(1,'old'); CREATE INDEX ix ON t(id);")
    db.execute("UPDATE t SET note='更新后的中文长内容' WHERE id=1;")
    assert rows(db, "SELECT note FROM t WHERE id=1;") == (("更新后的中文长内容",),)
    db.close()
    reopened = open_database(tmp_path / "db")
    try:
        assert rows(reopened, "SELECT note FROM t WHERE id=1;") == (("更新后的中文长内容",),)
    finally:
        reopened.close()


def test_referenced_parent_non_key_update(db):
    db.execute("CREATE TABLE p(id INT PRIMARY KEY, note VARCHAR); CREATE TABLE c(pid INT REFERENCES p(id)); INSERT INTO p(id,note) VALUES(1,'old'); INSERT INTO c(pid) VALUES(1);")
    db.execute("UPDATE p SET note='new' WHERE id=1;")
    assert rows(db, "SELECT note FROM p;") == (("new",),)
    with pytest.raises(MiniSQLError, match="FOREIGN_KEY_VIOLATION"):
        db.execute("UPDATE p SET id=2 WHERE id=1;")
    assert rows(db, "SELECT id FROM p;") == ((1,),)


@pytest.mark.parametrize("build_after", [False, True])
def test_composite_unique_index_null_semantics(db, build_after):
    db.execute("CREATE TABLE t(a INT,b INT);")
    if not build_after:
        db.execute("CREATE UNIQUE INDEX ix ON t(a,b);")
    db.execute("INSERT INTO t(a,b) VALUES(1,NULL); INSERT INTO t(a,b) VALUES(1,NULL);")
    if build_after:
        db.execute("CREATE UNIQUE INDEX ix ON t(a,b);")
    db.execute("INSERT INTO t(a,b) VALUES(1,2);")
    with pytest.raises(MiniSQLError, match="DUPLICATE_KEY"):
        db.execute("INSERT INTO t(a,b) VALUES(1,2);")
    assert rows(db, "SELECT COUNT(*) FROM t;") == ((3,),)


@pytest.mark.parametrize("predicate", [
    "a=1", "a=1 AND b>=2", "a=1 AND b<3", "a=1 AND b=2",
    "a>=1 AND a<2", "a=1 AND a=2", "a>0 AND b=2",
])
def test_composite_index_equals_scan(db, predicate):
    db.execute("CREATE TABLE t(a INT,b INT);")
    for a, b in [(0,0),(1,1),(1,2),(1,3),(2,1),(2,2)]:
        db.execute(f"INSERT INTO t(a,b) VALUES({a},{b});")
    sql = f"SELECT a,b FROM t WHERE {predicate} ORDER BY a,b;"
    expected = rows(db, sql)
    db.execute("CREATE INDEX ix ON t(a,b);")
    assert rows(db, sql) == expected


def test_update_unique_values_as_one_statement(db):
    db.execute("CREATE TABLE t(id INT PRIMARY KEY, label VARCHAR UNIQUE); INSERT INTO t(id,label) VALUES(1,'a'); INSERT INTO t(id,label) VALUES(2,'b'); CREATE UNIQUE INDEX ix ON t(id);")
    db.execute("UPDATE t SET id=3-id;")
    assert rows(db, "SELECT id,label FROM t ORDER BY id;") == ((1,"b"),(2,"a"))
    with pytest.raises(MiniSQLError, match="DUPLICATE_KEY"):
        db.execute("UPDATE t SET id=9;")
    assert rows(db, "SELECT id,label FROM t ORDER BY id;") == ((1,"b"),(2,"a"))


def test_add_drop_constraint_real_execution(db, tmp_path):
    db.execute("CREATE TABLE t(id INT); INSERT INTO t(id) VALUES(1); ALTER TABLE t ADD CONSTRAINT positive CHECK(id>0);")
    with pytest.raises(MiniSQLError, match="CHECK_VIOLATION"):
        db.execute("INSERT INTO t(id) VALUES(-1);")
    db.close()
    reopened = open_database(tmp_path / "db")
    try:
        with pytest.raises(MiniSQLError, match="CHECK_VIOLATION"):
            reopened.execute("INSERT INTO t(id) VALUES(-1);")
        reopened.execute("ALTER TABLE t DROP CONSTRAINT positive; INSERT INTO t(id) VALUES(-1);")
        assert rows(reopened, "SELECT id FROM t ORDER BY id;") == ((-1,),(1,))
    finally:
        reopened.close()


def test_add_constraint_checks_existing_rows(db):
    db.execute("CREATE TABLE t(id INT); INSERT INTO t(id) VALUES(-1);")
    with pytest.raises(MiniSQLError, match="CHECK_VIOLATION"):
        db.execute("ALTER TABLE t ADD CONSTRAINT positive CHECK(id>0);")
    assert rows(db, "SELECT id FROM t;") == ((-1,),)


def test_student_course_workflow(db, tmp_path):
    db.execute("CREATE TABLE students(id INT PRIMARY KEY, name VARCHAR NOT NULL); CREATE TABLE courses(id INT PRIMARY KEY, name VARCHAR); CREATE TABLE grades(sid INT REFERENCES students(id), cid INT REFERENCES courses(id), score DECIMAL(5,2) CHECK(score>=0 AND score<=100)); CREATE TABLE audit(sid INT, score DECIMAL(5,2));")
    db.execute("INSERT INTO students(id,name) VALUES(1,'小明'); INSERT INTO students(id,name) VALUES(2,'小红'); INSERT INTO courses(id,name) VALUES(1,'数据库'); INSERT INTO courses(id,name) VALUES(2,'编译原理');")
    db.execute("CREATE TRIGGER audit_grade AFTER INSERT ON grades FOR EACH ROW INSERT INTO audit(sid,score) VALUES(NEW.sid,NEW.score);")
    db.execute("BEGIN; INSERT INTO grades(sid,cid,score) VALUES(1,1,88.5); INSERT INTO grades(sid,cid,score) VALUES(1,2,91.5); INSERT INTO grades(sid,cid,score) VALUES(2,1,75); COMMIT;")
    assert rows(db, "SELECT s.name,SUM(g.score) AS total FROM students s JOIN grades g ON s.id=g.sid GROUP BY s.name ORDER BY total DESC;") == (("小明",Decimal("180")),("小红",Decimal("75")))
    db.execute("CREATE INDEX by_student ON grades(sid); CREATE VIEW high AS SELECT sid,score FROM grades WHERE score>=80;")
    assert rows(db, "SELECT id FROM students s WHERE EXISTS(SELECT sid FROM grades g WHERE g.sid=s.id AND g.score>=90);") == ((1,),)
    db.execute("BEGIN; UPDATE grades SET score=0 WHERE sid=1; ROLLBACK;")
    assert rows(db, "SELECT score FROM grades WHERE sid=1 ORDER BY score;") == ((Decimal("88.5"),),(Decimal("91.5"),))
    with pytest.raises(MiniSQLError, match="FOREIGN_KEY_VIOLATION"):
        db.execute("DELETE FROM students WHERE id=1;")
    db.execute("CREATE USER admin IDENTIFIED BY 'acceptance-admin';")
    assert db.login("admin", "acceptance-admin")
    db.execute("CREATE USER reader IDENTIFIED BY 'acceptance-reader'; GRANT SELECT ON VIEW high TO reader; GRANT SELECT ON TABLE grades TO reader;")
    assert db.login("reader", "acceptance-reader")
    assert len(rows(db, "SELECT * FROM high;")) == 2
    with pytest.raises(MiniSQLError, match="PERMISSION_DENIED"):
        db.execute("SELECT sid FROM grades WHERE EXISTS(SELECT sid FROM audit);")
    db.close()
    reopened = open_database(tmp_path / "db")
    try:
        assert reopened.login("reader", "acceptance-reader")
        assert len(rows(reopened, "SELECT * FROM high;")) == 2
        assert reopened.login("admin", "acceptance-admin")
        assert rows(reopened, "SELECT COUNT(*) FROM audit;") == ((3,),)
        reopened.execute("INSERT INTO grades(sid,cid,score) VALUES(2,2,80);")
        assert rows(reopened, "SELECT COUNT(*) FROM audit;") == ((4,),)
    finally:
        reopened.close()


def test_index_query_does_not_scan_heap(db, monkeypatch):
    from minisql.storage.record import HeapStorage
    db.execute("CREATE TABLE t(id INT, note VARCHAR); INSERT INTO t(id,note) VALUES(1,'one'); CREATE INDEX ix ON t(id);")
    original = HeapStorage.scan

    def guarded_scan(storage, schema):
        assert schema.name != "t", "索引回表不应扫描全部用户记录"
        yield from original(storage, schema)

    monkeypatch.setattr(HeapStorage, "scan", guarded_scan)
    assert rows(db, "SELECT note FROM t WHERE id=1;") == (("one",),)


def test_record_fetch_rejects_cross_table_and_deleted_slots(db):
    db.execute("CREATE TABLE a(id INT); CREATE TABLE b(id INT); INSERT INTO a(id) VALUES(1); INSERT INTO a(id) VALUES(2); INSERT INTO b(id) VALUES(3);")
    db.execute("BEGIN;")
    a, b = db.catalog.get_table("a"), db.catalog.get_table("b")
    record = next(db.storage.scan(a))
    assert db.storage.fetch(a, record.record_id).row == (1,)
    with pytest.raises(MiniSQLError, match="INVALID_RECORD"):
        db.storage.fetch(b, record.record_id)
    db.storage.delete(a, record.record_id)
    with pytest.raises(MiniSQLError, match="INVALID_RECORD"):
        db.storage.fetch(a, record.record_id)
    db.rollback()


def test_trigger_action_failure_restores_table_and_index(db):
    db.execute("CREATE TABLE t(id INT); CREATE INDEX ix ON t(id); CREATE TABLE audit(id INT PRIMARY KEY); INSERT INTO audit(id) VALUES(7); CREATE TRIGGER tr AFTER INSERT ON t FOR EACH ROW INSERT INTO audit(id) VALUES(NEW.id);")
    with pytest.raises(MiniSQLError, match="DUPLICATE_KEY"):
        db.execute("INSERT INTO t(id) VALUES(7);")
    assert rows(db, "SELECT id FROM t WHERE id=7;") == ()
    assert rows(db, "SELECT id FROM audit;") == ((7,),)


def test_decimal_integer_assignment_and_notnull_after_restart(db, tmp_path):
    db.execute("CREATE TABLE t(id INT, amount DECIMAL(8,2), name VARCHAR NOT NULL); INSERT INTO t(id,amount,name) VALUES(1,2,'x');")
    db.close()
    reopened = open_database(tmp_path / "db")
    try:
        reopened.execute("UPDATE t SET amount=3;")
        assert rows(reopened, "SELECT amount FROM t;") == ((Decimal('3.00'),),)
        with pytest.raises(MiniSQLError, match="NOT_NULL_VIOLATION"):
            reopened.execute("UPDATE t SET name=NULL;")
    finally:
        reopened.close()


def test_split_composite_index_range_and_relocation(db, tmp_path):
    db.execute("CREATE TABLE t(a INT,b INT,note VARCHAR); BEGIN;")
    for i in range(600):
        db.execute(f"INSERT INTO t(a,b,note) VALUES({i//100},{i%100},'r{i}');")
    db.execute("COMMIT;")
    conditions = ["a=3", "a=3 AND b>40 AND b<=45", "a>3", "a<2", "a>=2 AND a<=3", "a=3 AND a=4", "a=3 AND b=41 AND b=42"]
    expected = {condition: rows(db, f"SELECT a,b FROM t WHERE {condition} ORDER BY a,b;") for condition in conditions}
    db.execute("CREATE INDEX ix ON t(a,b); UPDATE t SET note='变长内容改变物理记录位置' WHERE a=3; BEGIN; DELETE FROM t WHERE a=3; ROLLBACK;")
    db.close()
    reopened = open_database(tmp_path / "db")
    try:
        for condition in conditions:
            assert rows(reopened, f"SELECT a,b FROM t WHERE {condition} ORDER BY a,b;") == expected[condition]
        assert len(rows(reopened, "SELECT note FROM t WHERE a=3;")) == 100
    finally:
        reopened.close()


@pytest.mark.parametrize("committed", [False, True])
def test_sql_index_trigger_crash_recovery(db, tmp_path, committed):
    db.execute("CREATE TABLE t(id INT,note VARCHAR); INSERT INTO t(id,note) VALUES(1,'old'); CREATE INDEX ix ON t(id); CREATE TABLE audit(id INT); CREATE TRIGGER tr AFTER UPDATE ON t FOR EACH ROW INSERT INTO audit(id) VALUES(NEW.id);")
    db.close()
    script = """
import os, sys
from minisql.engine.database import open_database
db = open_database(sys.argv[1])
db.execute("BEGIN; UPDATE t SET note='new' WHERE id=1;")
db.storage.flush()
if sys.argv[2] == 'yes':
    db.commit()
os._exit(71)
"""
    child = subprocess.run([sys.executable, "-X", "utf8", "-c", script,
                            str(tmp_path / "db"), "yes" if committed else "no"],
                           cwd=Path(__file__).resolve().parents[2], capture_output=True,
                           text=True, timeout=20)
    assert child.returncode == 71, child.stderr
    reopened = open_database(tmp_path / "db")
    try:
        assert rows(reopened, "SELECT note FROM t WHERE id=1;") == (("new" if committed else "old",),)
        assert rows(reopened, "SELECT COUNT(*) FROM audit;") == ((1 if committed else 0,),)
    finally:
        reopened.close()


def test_demo_script_utf8_subprocesses(tmp_path):
    project = Path(__file__).resolve().parents[2]
    child = subprocess.run([sys.executable, "-X", "utf8", str(project / "examples/demo.py")],
                           cwd=project, capture_output=True, text=True, encoding="utf-8",
                           env={**os.environ, "PYTHONUTF8": "0", "TEMP": str(tmp_path),
                                "TMP": str(tmp_path)}, timeout=30)
    assert child.returncode == 0, child.stderr
    assert not child.stderr
    assert "预期错误 UNKNOWN_COLUMN 诊断正确" in child.stdout
    assert "Alice" in child.stdout and "Bob" in child.stdout


@pytest.mark.parametrize("sql,expected", [
    ("SELECT a.id,a.v FROM a INNER JOIN b ON a.id=b.id;", ((2,20),)),
    ("SELECT a.id,b.id FROM a LEFT JOIN b ON a.id=b.id ORDER BY a.id;", ((1,None),(2,2),(3,None))),
    ("SELECT a.id,b.id FROM a RIGHT JOIN b ON a.id=b.id ORDER BY b.id;", ((2,2),(None,4))),
    ("SELECT COUNT(*) FROM a CROSS JOIN b;", ((6,),)),
    ("SELECT COUNT(*),COUNT(v),SUM(v),AVG(v),MIN(v),MAX(v) FROM a;", ((3,2,30,Decimal('15'),10,20),)),
    ("SELECT g,SUM(v) AS total FROM a GROUP BY g HAVING SUM(v)>10 ORDER BY g;", ((1,30),)),
    ("SELECT COUNT(*),SUM(v),AVG(v) FROM a WHERE id=99;", ((0,None,None),)),
    ("SELECT id FROM a UNION SELECT id FROM b ORDER BY id;", ((1,),(2,),(3,),(4,))),
    ("SELECT id FROM a UNION ALL SELECT id FROM b ORDER BY id;", ((1,),(2,),(2,),(3,),(4,))),
    ("SELECT id FROM a INTERSECT SELECT id FROM b;", ((2,),)),
    ("SELECT id FROM a EXCEPT SELECT id FROM b ORDER BY id;", ((1,),(3,))),
    ("SELECT q.id FROM (SELECT id FROM a WHERE v>10) q;", ((2,),)),
    ("SELECT id FROM a WHERE id IN(SELECT id FROM b);", ((2,),)),
    ("SELECT id FROM a WHERE id NOT IN(2,NULL);", ()),
    ("SELECT (SELECT id FROM b WHERE id=99) AS missing FROM a WHERE id=1;", ((None,),)),
    ("SELECT id FROM a WHERE EXISTS(SELECT id FROM b WHERE b.id=a.id);", ((2,),)),
])
def test_extended_query_result_matrix(db, sql, expected):
    db.execute("CREATE TABLE a(id INT,v INT,g INT); CREATE TABLE b(id INT); INSERT INTO a(id,v,g) VALUES(1,10,1); INSERT INTO a(id,v,g) VALUES(2,20,1); INSERT INTO a(id,v,g) VALUES(3,NULL,2); INSERT INTO b(id) VALUES(2); INSERT INTO b(id) VALUES(4);")
    assert rows(db, sql) == expected


def test_scalar_subquery_multiple_rows_rejected(db):
    db.execute("CREATE TABLE t(id INT); INSERT INTO t(id) VALUES(1); INSERT INTO t(id) VALUES(2);")
    with pytest.raises(MiniSQLError, match="SUBQUERY_MULTIPLE_ROWS"):
        db.execute("SELECT (SELECT id FROM t) FROM t;")


def test_sql_new_types_reopen_and_alter(db, tmp_path):
    from datetime import date, time, datetime
    db.execute("CREATE TABLE t(id INT,d DATE,tm TIME,ts TIMESTAMP,flag BOOL); INSERT INTO t(id,d,tm,ts,flag) VALUES(1,DATE '2024-02-29',TIME '23:59:59',TIMESTAMP '2024-02-29 23:59:59',TRUE);")
    db.execute("ALTER TABLE t ALTER COLUMN id TYPE DECIMAL(8,2);")
    db.close()
    reopened = open_database(tmp_path / "db")
    try:
        assert rows(reopened, "SELECT * FROM t;") == ((Decimal('1.00'),date(2024,2,29),time(23,59,59),datetime(2024,2,29,23,59,59),True),)
    finally:
        reopened.close()
