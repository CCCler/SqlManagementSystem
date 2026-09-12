"""真实数据库/GUI 保持旧业务语义，扩展计划只读展示与密码变更回归。"""
import json
import pytest
from minisql.engine.database import open_database
from minisql.gui.session import Session, CaptureCompiler
from minisql.compiler.compiler import SQLCompiler
from minisql.contracts.errors import MiniSQLError
from tests.fakes.memory import ExtendedMemoryCatalog

@pytest.mark.parametrize('sql',[
 "ALTER USER alice IDENTIFIED BY 'newpw';",
])
def test_password_change_preserves_business_data(tmp_path,sql):
    db=open_database(tmp_path)
    try:
        db.execute('CREATE TABLE t(id INT); INSERT INTO t(id) VALUES (1);')
        db.execute("CREATE USER alice IDENTIFIED BY 'pw';")  # 初始化模式创建首个账户
        assert db.login('alice','pw')
        before=(tmp_path/'minisql.db').read_bytes()
        db.execute(sql)
        assert not db.login('alice','pw')
        assert db.login('alice','newpw')
        assert (tmp_path/'minisql.db').read_bytes()!=before
        assert db.execute('SELECT * FROM t;')[0].rows==((1,),)
    finally: db.close()
    db=open_database(tmp_path)
    try:
        assert db.login('alice','newpw')
        assert db.execute('SELECT * FROM t;')[0].rows==((1,),)
    finally: db.close()

def test_failed_batch_and_transaction(tmp_path):
    db=open_database(tmp_path)
    duplicate='INSERT INTO t(id) VALUES(1);'  # 主键冲突在运行期触发，批次停止
    try:
        with pytest.raises(MiniSQLError):
            db.execute('CREATE TABLE t(id INT PRIMARY KEY); INSERT INTO t(id) VALUES(1);'
                       ' '+duplicate+' INSERT INTO t(id) VALUES(2);')
        assert db.execute('SELECT * FROM t;')[0].rows==((1,),)
        db.execute('BEGIN; INSERT INTO t(id) VALUES(3);')
        with pytest.raises(MiniSQLError,match='DUPLICATE_KEY'): db.execute(duplicate)
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
        assert '仅展示，不执行' in data['results'][0]['message']
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
