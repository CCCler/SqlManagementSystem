"""对象目录恢复与 SQL 系统表隔离回归。"""
from pathlib import Path
import pytest
from minisql.engine.database import open_database
from minisql.engine.catalog import PersistentCatalog
from minisql.engine.objects import ViewDefinition,ConstraintDefinition,IndexDefinition
from minisql.contracts.models import ColumnSchema as C,TableSchema as T,DataType as D
from minisql.contracts.errors import MiniSQLError
from minisql.storage.file_manager import FileManager
from minisql.storage.page import DiskPageManager
from minisql.storage.buffer import PageBufferPool
from minisql.storage.record import HeapStorage

@pytest.mark.parametrize('sql',[
 'SELECT * FROM __users;', 'SELECT salt FROM __users;',
 'SELECT a.* FROM __users a;',
 'SELECT id FROM t WHERE EXISTS(SELECT * FROM __users);',
 'EXPLAIN SELECT * FROM __users;', 'DELETE FROM __users;',
 'DROP TABLE __users;', "UPDATE __users SET user_name='x';",
 "CREATE TABLE __new(id INT);",
])
def test_internal_sql_rejected(tmp_path,sql):
    db=open_database(tmp_path)
    try:
        db.execute('CREATE TABLE t(id INT);')
        with pytest.raises(MiniSQLError,match='PROTECTED_TABLE'):db.execute(sql)
        assert db.catalog.get_table('__users') is not None
    finally:db.close()

def test_view_and_constraint_reopen(tmp_path):
    db=open_database(tmp_path)
    try:
        db.execute('CREATE TABLE t(a INT,b INT);')
        db.objects.register_constraint(ConstraintDefinition('t','uq','UNIQUE',('b','a')))
        db.objects.register_view(ViewDefinition('v','SELECT a FROM t;',(C('a',D.DECIMAL,10,2),)))
    finally:db.close()
    for _ in range(2):
        db=open_database(tmp_path)
        try:
            assert db.objects.get_constraints('t')[0].columns==('b','a')
            c=db.objects.get_view('v').columns[0]
            assert (c.precision,c.scale)==(10,2)
            assert 'ViewScan' in db.execute('EXPLAIN SELECT * FROM v;')[0].message
            assert db.execute('SELECT * FROM v;')[0].rows == ()
            with pytest.raises(MiniSQLError,match='READ_ONLY_VIEW'):db.execute('DELETE FROM v;')
        finally:db.close()

def test_legacy_index_catalog_empty_and_nonempty(tmp_path):
    pages=DiskPageManager(FileManager(tmp_path/'minisql.db'))
    storage=HeapStorage(pages,PageBufferPool(pages))
    try:
        cat=PersistentCatalog(storage);cat.bootstrap()
        old=storage.create_table(T('__indexes',(C('index_id',D.INT),C('index_name',D.VARCHAR),C('table_name',D.VARCHAR),C('unique_flag',D.INT),C('column_index',D.INT),C('column_name',D.VARCHAR))))
        cat.register_table(old)
        storage.insert(old,(1,'i','t',0,0,'a'))
    finally:storage.close()
    for n in range(2):
        db=open_database(tmp_path)
        try:
            assert db.objects.get_index('i').root_page is None
            if n==0:
                with pytest.raises(MiniSQLError,match='CATALOG_UPGRADE_REQUIRED'):
                    db.objects.register_index(IndexDefinition('j','t',('a',)))
                with pytest.raises(MiniSQLError,match='CATALOG_UPGRADE_REQUIRED'):
                    db.objects.register_index(IndexDefinition('k','t',('a',),root_page=42))
            else:assert db.objects.get_index('j') is None
        finally:db.close()

def test_view_cannot_expose_system_table(tmp_path):
    db=open_database(tmp_path)
    try:
        db.objects.register_view(ViewDefinition('leak','SELECT salt FROM __users;',(C('salt',D.VARCHAR),)))
        with pytest.raises(MiniSQLError,match='PROTECTED_TABLE'):db.execute('EXPLAIN SELECT * FROM leak;')
    finally:db.close()


def test_gui_system_table_never_reaches_results_or_trace(tmp_path):
    from minisql.gui.session import Session
    gui=Session(tmp_path)
    try:
        gui.submit('connect',{}).result()
        result=gui.submit('execute',{'sql':'SELECT * FROM __users;'}).result()
        assert not result['ok'] and result['error']['code']=='PROTECTED_TABLE'
        assert result['results']==[] and result['compilations']==[]
    finally:gui.close()

def test_constraint_and_view_rollback(tmp_path):
    db=open_database(tmp_path)
    try:
        db.execute('CREATE TABLE t(a INT,b INT); BEGIN;')
        db.objects.register_constraint(ConstraintDefinition('t','uq','UNIQUE',('b','a')))
        db.objects.register_view(ViewDefinition('v','SELECT a FROM t;',(C('a',D.DECIMAL,10,2),)))
        db.rollback()
        assert db.objects.get_constraints('t')==()
        assert db.objects.get_view('v') is None
    finally:db.close()

def test_constraint_order_survives_reclaimed_slots(tmp_path):
    db=open_database(tmp_path)
    try:
        db.execute('CREATE TABLE t(a INT,b INT,c INT);')
        db.objects.register_constraint(ConstraintDefinition('t','old','UNIQUE',('a',)))
        db.objects.register_constraint(ConstraintDefinition('t','keep','UNIQUE',('b',)))
        db.objects.unregister_constraints('t','old')
        db.objects.register_constraint(ConstraintDefinition('t','newc','UNIQUE',('c','a','b')))
    finally:db.close()
    db=open_database(tmp_path)
    try:assert next(c for c in db.objects.get_constraints('t') if c.name=='newc').columns==('c','a','b')
    finally:db.close()
