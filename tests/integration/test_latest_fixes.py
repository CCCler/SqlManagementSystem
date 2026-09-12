"""最新执行接入问题的真实文件回归。"""
import json
import pytest
from minisql.engine.database import open_database
from minisql.contracts.errors import MiniSQLError

@pytest.fixture
def db(tmp_path):
    db = open_database(tmp_path)
    db.execute("CREATE TABLE t(id INT); INSERT INTO t(id) VALUES(1); CREATE TABLE secret(id INT); INSERT INTO secret(id) VALUES(9);")
    db.execute("CREATE USER admin IDENTIFIED BY 'admin-test';")
    assert db.login('admin','admin-test')
    db.execute("CREATE USER alice IDENTIFIED BY 'alice-test'; GRANT SELECT ON TABLE t TO alice;")
    yield db
    db.close()

@pytest.mark.parametrize('sql',[
 'SELECT id FROM t WHERE id=1 AND EXISTS(SELECT id FROM secret);',
 'SELECT id FROM t WHERE NOT (id=2 OR EXISTS(SELECT id FROM secret));',
 'SELECT id+(SELECT id FROM secret) FROM t;',
 'SELECT id FROM t WHERE id IN (1,(SELECT id FROM secret));',
 'EXPLAIN SELECT id FROM t WHERE id=1 AND EXISTS(SELECT id FROM secret);',
])
def test_nested_query_permissions(db,sql):
    assert db.login('alice','alice-test')
    with pytest.raises(MiniSQLError,match='PERMISSION_DENIED'): db.execute(sql)
    assert db.login('admin','admin-test')
    db.execute('GRANT SELECT ON TABLE secret TO alice;')
    assert db.login('alice','alice-test')
    db.execute(sql)

def test_database_and_object_grants(db):
    db.execute('GRANT CREATE TABLE,CREATE INDEX,CREATE VIEW ON DATABASE main TO alice;')
    assert db.login('alice','alice-test')
    db.execute('CREATE TABLE own(id INT); CREATE INDEX ix ON t(id); CREATE VIEW v AS SELECT id FROM t;')
    with pytest.raises(MiniSQLError,match='PERMISSION_DENIED'): db.execute('SELECT * FROM v;')
    assert db.login('admin','admin-test')
    db.execute('GRANT SELECT,DROP ON VIEW v TO alice; GRANT DROP ON INDEX ix TO alice; REVOKE CREATE TABLE ON DATABASE main FROM alice;')
    assert db.login('alice','alice-test')
    assert db.execute('SELECT * FROM v;')[0].rows == ((1,),)
    db.execute('DROP VIEW v; DROP INDEX ix;')
    with pytest.raises(MiniSQLError,match='PERMISSION_DENIED'): db.execute('CREATE TABLE denied(id INT);')

def test_password_sessions_rollback_and_reopen(db,tmp_path):
    other = open_database(tmp_path)
    try:
        assert other.login('alice','alice-test')
        db.execute("BEGIN; ALTER USER alice IDENTIFIED BY 'changed-test'; ROLLBACK;")
        assert other.login('alice','alice-test')
        db.execute("ALTER USER alice IDENTIFIED BY 'changed-test';")
        with pytest.raises(MiniSQLError,match='PERMISSION_DENIED'): other.execute('SELECT * FROM t;')
        assert not other.login('alice','alice-test')
        assert other.login('alice','changed-test')
        with pytest.raises(MiniSQLError,match='PERMISSION_DENIED'):
            other.execute("ALTER USER admin IDENTIFIED BY 'forbidden-test';")
        other.execute("ALTER USER alice IDENTIFIED BY 'self-test';")
    finally: other.close()
    other = open_database(tmp_path)
    try:
        assert other.login('alice','self-test')
        assert other.execute('SELECT * FROM t;')[0].rows == ((1,),)
        assert not other.login('alice','changed-test')
        assert other.login('admin','admin-test')
    finally: other.close()

def test_view_decimal_and_explain(tmp_path):
    db = open_database(tmp_path)
    try:
        db.execute('CREATE TABLE d(x DECIMAL(10,2)); INSERT INTO d(x) VALUES(12.34); CREATE VIEW v AS SELECT x FROM d;')
        message=db.execute('EXPLAIN UPDATE d SET x=x*2;')[0].message
        assert '仅展示，不执行' in message and '执行待接入' not in message
        assert str(db.execute('SELECT x FROM d;')[0].rows[0][0])=='12.34'
    finally: db.close()
    db=open_database(tmp_path)
    try:
        c=db.objects.get_view('v').columns[0]
        assert (c.precision,c.scale)==(10,2)
        assert str(db.execute('SELECT x FROM v;')[0].rows[0][0])=='12.34'
    finally: db.close()

def test_trigger_nested_query_denied_and_rolled_back(db):
    db.execute('CREATE TRIGGER tr AFTER INSERT ON t FOR EACH ROW SELECT id FROM t WHERE id=1 AND EXISTS(SELECT id FROM secret);')
    db.execute('GRANT INSERT ON TABLE t TO alice;')
    assert db.login('alice','alice-test')
    with pytest.raises(MiniSQLError,match='PERMISSION_DENIED'):
        db.execute('INSERT INTO t(id) VALUES(2);')
    assert db.execute('SELECT id FROM t;')[0].rows == ((1,),)

def test_gui_password_change_redacted(tmp_path):
    from minisql.gui.session import Session
    gui=Session(tmp_path)
    try:
        assert gui.submit('connect',{}).result()['ok']
        assert gui.submit('execute',{'sql':"CREATE USER admin IDENTIFIED BY 'gui-old';"}).result()['ok']
        assert gui.submit('login',{'user':'admin','password':'gui-old'}).result()['ok']
        reply=gui.submit('execute',{'sql':"ALTER USER admin IDENTIFIED BY 'GUI_PASSWORD_SENTINEL';"}).result()
        assert reply['ok']
        assert 'GUI_PASSWORD_SENTINEL' not in json.dumps(reply)
        assert not gui.submit('login',{'user':'admin','password':'gui-old'}).result()['ok']
        assert gui.submit('login',{'user':'admin','password':'GUI_PASSWORD_SENTINEL'}).result()['ok']
    finally: gui.close()
