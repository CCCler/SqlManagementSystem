"""UPDATE 的编译、真实存储和事务回归。"""
import pytest
from minisql.contracts.errors import MiniSQLError
from minisql.engine.database import open_database
from minisql.gui.session import Session
from minisql.storage.record import HeapStorage


@pytest.fixture
def db(tmp_path):
    connection = open_database(tmp_path)
    connection.execute("CREATE TABLE t(id INT, age INT, name VARCHAR);"
                       "INSERT INTO t(id,age,name) VALUES (1,20,'甲');"
                       "INSERT INTO t(id,age,name) VALUES (2,30,'乙');")
    yield connection
    connection.close()


def rows(db):
    return db.execute("SELECT * FROM t ORDER BY id;")[0].rows


def test_multi_assignment_uses_old_row_and_counts_matches(db):
    assert db.execute("uPdAtE t SeT id=age, age=id, name='O''Brien' WHERE id=1;")[0].affected_rows == 1
    assert rows(db) == ((2,30,'乙'), (20,1,"O'Brien"))
    assert db.execute("UPDATE t SET age=age;")[0].affected_rows == 2
    assert db.execute("UPDATE t SET age=age+1 WHERE id=999;")[0].affected_rows == 0
    assert db.execute("UPDATE t SET age=age+1 WHERE FALSE;")[0].affected_rows == 0
    assert db.execute("UPDATE t SET age=age+1;")[0].affected_rows == 2
    assert rows(db) == ((2,31,'乙'), (20,2,"O'Brien"))


@pytest.mark.parametrize('sql,code', [
    ("UPDATE absent SET id=1;", 'UNKNOWN_TABLE'),
    ("UPDATE t SET absent=1;", 'UNKNOWN_COLUMN'),
    ("UPDATE t SET age=absent;", 'UNKNOWN_COLUMN'),
    ("UPDATE t SET age=1, AGE=2;", 'DUPLICATE_COLUMN'),
    ("UPDATE t SET age='x';", 'TYPE_MISMATCH'),
    ("UPDATE t SET age=TRUE;", 'TYPE_MISMATCH'),
    ("UPDATE t SET name=age;", 'TYPE_MISMATCH'),
    ("UPDATE t SET age=1 WHERE age;", 'TYPE_MISMATCH'),
    ("UPDATE t SET age=9223372036854775808;", 'INTEGER_OUT_OF_RANGE'),
    ("UPDATE __catalog SET id=1;", 'PROTECTED_TABLE'),
    ("UPDATE t SET;", 'UNEXPECTED_TOKEN'),
    ("UPDATE t SET age=;", 'UNEXPECTED_TOKEN'),
    ("UPDATE t SET age=1,;", 'UNEXPECTED_TOKEN'),
    ("UPDATE t SET age=1", 'UNEXPECTED_TOKEN'),
    ("UPDATE t SET age=1 LIMIT 1;", 'UNEXPECTED_TOKEN'),
    ("UPDATE t SET age="+'('*65+'1'+')'*65+';', 'EXPRESSION_TOO_COMPLEX'),
])
def test_reject_invalid_update_without_changes(db, sql, code):
    before = rows(db)
    with pytest.raises(MiniSQLError) as caught:
        db.execute(sql)
    assert caught.value.code == code
    assert caught.value.position is not None
    assert rows(db) == before


def test_commit_rollback_close_and_persistence(db, tmp_path):
    before = rows(db)
    db.execute("BEGIN; UPDATE t SET age=age+1; ROLLBACK;")
    assert rows(db) == before
    db.execute("BEGIN; UPDATE t SET age=age+2; COMMIT;")
    committed = rows(db)
    db.execute("BEGIN; UPDATE t SET name='pending';")
    db.close()
    reopened = open_database(tmp_path)
    try:
        assert rows(reopened) == committed
    finally:
        reopened.close()


def test_overflow_and_explicit_failure(db):
    db.execute("UPDATE t SET age=9223372036854775807 WHERE id=2;")
    before = rows(db)
    with pytest.raises(MiniSQLError, match='INTEGER_OUT_OF_RANGE'):
        db.execute("UPDATE t SET age=age+1;")
    assert rows(db) == before
    db.execute('BEGIN;')
    with pytest.raises(MiniSQLError, match='INTEGER_OUT_OF_RANGE'):
        db.execute("UPDATE t SET age=age+1;")
    with pytest.raises(MiniSQLError, match='TRANSACTION_ABORTED'):
        db.execute('COMMIT;')
    db.rollback()
    assert rows(db) == before


def test_partial_storage_failure_restores_entire_update(db, monkeypatch):
    before = rows(db)
    original = HeapStorage.insert
    calls = 0
    def fail_second(storage, schema, row):
        nonlocal calls
        if schema.name == 't':
            calls += 1
            if calls == 2:
                raise OSError('injected update failure')
        return original(storage, schema, row)
    with monkeypatch.context() as patch:
        patch.setattr(HeapStorage, 'insert', fail_second)
        with pytest.raises((MiniSQLError, OSError)):
            db.execute('UPDATE t SET age=age+1;')
    assert calls == 2
    assert rows(db) == before


def test_cross_page_growth_shrink_and_no_repeated_updates(db, tmp_path):
    db.execute('BEGIN;')
    for i in range(3,43):
        db.execute(f"INSERT INTO t(id,age,name) VALUES ({i},0,'x');")
    db.commit()
    value = '中文'*450
    result = db.execute(f"UPDATE t SET name='{value}', age=age+1;")[0]
    assert result.affected_rows == 42
    actual = rows(db)
    assert len(actual) == 42 and all(r[2] == value for r in actual)
    assert [r[1] for r in actual] == [21,31]+[1]*40
    db.execute("UPDATE t SET name='';")
    before = rows(db)
    with pytest.raises(MiniSQLError):
        db.execute("UPDATE t SET name='"+'x'*10000+"';")
    assert rows(db) == before
    db.close()
    reopened = open_database(tmp_path)
    try:
        assert rows(reopened) == before
    finally:
        reopened.close()


def test_batch_stops_but_previous_autocommit_remains(db):
    with pytest.raises(MiniSQLError):
        db.execute("UPDATE t SET age=99 WHERE id=1; UPDATE t SET age='bad'; DELETE FROM t;")
    assert rows(db) == ((1,99,'甲'),(2,30,'乙'))


def test_explain_and_gui_compilation_execute_once(tmp_path):
    session = Session(tmp_path)
    def run(sql):
        return session.submit('execute', {'sql':sql}).result()
    try:
        session.submit('connect', {}).result()
        assert run('CREATE TABLE t(id INT); INSERT INTO t(id) VALUES (1);')['ok']
        data = run('UPDATE t SET id=id+1 WHERE 1=1;')
        assert data['ok'] and data['results'][0]['affected_rows'] == 1
        assert len(data['compilations']) == 1
        trace = data['compilations'][0]
        assert trace['ast']['node'] == 'UpdateStmt'
        assert trace['plan']['node'] == 'Update'
        assert trace['optimized_plan']['source']['node'] == 'SeqScan'
        explained = run('EXPLAIN UPDATE t SET id=id+1 WHERE FALSE;')
        assert 'EmptyScan' in explained['results'][0]['message']
        assert run('SELECT * FROM t;')['results'][0]['rows'] == [[2]]
    finally:
        session.close()
