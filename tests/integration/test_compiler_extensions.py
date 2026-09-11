"""真实数据库/GUI 保持旧业务语义，扩展计划只读展示并拒绝写入。"""
import json
import pytest
from minisql.engine.database import open_database
from minisql.gui.session import Session, CaptureCompiler
from minisql.compiler.compiler import SQLCompiler
from minisql.contracts.errors import MiniSQLError
from tests.fakes.memory import ExtendedMemoryCatalog

@pytest.mark.parametrize('sql',[
 'CREATE TRIGGER tr AFTER INSERT ON t FOR EACH ROW INSERT INTO u(id) VALUES (NEW.id);',
 'CREATE TRIGGER tr2 AFTER DELETE ON t FOR EACH ROW INSERT INTO u(id) VALUES (OLD.id);',
])
def test_guard_no_business_change(tmp_path,sql):
    db=open_database(tmp_path)
    try:
        db.execute('CREATE TABLE t(id INT); INSERT INTO t(id) VALUES (1);')
        db.execute('CREATE TABLE u(id INT);')
        before=(tmp_path/'minisql.db').read_bytes()
        with pytest.raises(MiniSQLError,match='FEATURE_NOT_EXECUTABLE'): db.execute(sql)
        assert (tmp_path/'minisql.db').read_bytes()==before
        assert db.execute('SELECT * FROM t;')[0].rows==((1,),)
    finally: db.close()
    db=open_database(tmp_path)
    try: assert db.execute('SELECT * FROM t;')[0].rows==((1,),)
    finally: db.close()

def test_failed_batch_and_transaction(tmp_path):
    db=open_database(tmp_path)
    gated='CREATE TRIGGER tr AFTER INSERT ON t FOR EACH ROW INSERT INTO u(id) VALUES (NEW.id);'
    try:
        with pytest.raises(MiniSQLError):
            db.execute('CREATE TABLE t(id INT); INSERT INTO t(id) VALUES(1);'
                       ' CREATE TABLE u(id INT); '+gated+' INSERT INTO t(id) VALUES(2);')
        assert db.execute('SELECT * FROM t;')[0].rows==((1,),)
        db.execute('BEGIN; INSERT INTO t(id) VALUES(3);')
        with pytest.raises(MiniSQLError,match='FEATURE_NOT_EXECUTABLE'): db.execute(gated)
        with pytest.raises(MiniSQLError,match='TRANSACTION_ABORTED'): db.execute('COMMIT;')
        db.execute('ROLLBACK;')
        assert db.execute('SELECT * FROM t;')[0].rows==((1,),)
    finally: db.close()

def test_gui_explain_compiled_once(tmp_path):
    session=Session(tmp_path)
    try:
        session.submit('connect',{}).result()
        session.submit('execute',{'sql':'CREATE TABLE t(id INT); INSERT INTO t(id) VALUES(1);'}).result()
        data=session.submit('execute',{'sql':'EXPLAIN UPDATE t SET id=id*2;'}).result()
        assert data['ok']
        assert len(data['compilations'])==1
        assert '执行待接入' in data['results'][0]['message']
        assert data['compilations'][0]['required_capabilities']
        data=session.submit('execute',{'sql':'SELECT * FROM t;'}).result()
        assert data['results'][0]['rows']==[[1]]
    finally: session.close()

def test_gui_password_capture():
    compiler=CaptureCompiler(SQLCompiler())
    compiler.compile("CREATE USER bob IDENTIFIED BY 'PASSWORD_SENTINEL';", ExtendedMemoryCatalog())
    assert 'PASSWORD_SENTINEL' not in json.dumps(compiler.events)


def test_explain_create_never_writes(tmp_path):
    db=open_database(tmp_path)
    try:
        before=(tmp_path/'minisql.db').read_bytes()
        result=db.execute('EXPLAIN CREATE TABLE fresh(id INT PRIMARY KEY);')[0]
        assert 'CreateTable' in result.message
        assert (tmp_path/'minisql.db').read_bytes()==before
        assert db.catalog.get_table('fresh') is None
    finally: db.close()
