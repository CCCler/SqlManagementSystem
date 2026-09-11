"""真实目录：DECIMAL 参数恢复、旧裸类型兼容和事务回滚。"""
from decimal import Decimal
import pytest
from minisql.engine.database import open_database
from minisql.engine.catalog import SYSTEM_CATALOG, PersistentCatalog
from minisql.contracts.models import ColumnSchema as C, TableSchema as T, DataType as D
from minisql.contracts.errors import MiniSQLError
from tests.fakes.memory import MemoryStorage

@pytest.mark.parametrize('precision,scale',[(10,2),(38,0),(38,38),(1,0),(18,2),(None,None)])
def test_decimal_reopen(tmp_path,precision,scale):
    db=open_database(tmp_path)
    try:
        db.begin()
        schema=db.storage.create_table(T('money',(C('amount',D.DECIMAL,precision,scale),)))
        db.catalog.register_table(schema)
        db.storage.insert(schema,(Decimal('0'),))
        db.commit()
    finally: db.close()
    for _ in range(2):
        db=open_database(tmp_path)
        try:
            recovered=db.catalog.get_table('money')
            assert recovered==schema
            assert [r.row for r in db.storage.scan(recovered)]==[(Decimal('0'),)]
        finally: db.close()

def test_register_rollback_and_old_sql(tmp_path):
    db=open_database(tmp_path)
    try:
        db.execute("CREATE TABLE t(id INT,name VARCHAR); INSERT INTO t(id,name) VALUES(1,'old');")
        db.begin()
        schema=db.storage.create_table(T('money',(C('d',D.DECIMAL,10,2),)))
        db.catalog.register_table(schema)
        db.rollback()
        assert db.catalog.get_table('money') is None
    finally: db.close()
    db=open_database(tmp_path)
    try:
        assert db.catalog.get_table('money') is None
        assert db.execute('SELECT * FROM t;')[0].rows==((1,'old'),)
    finally: db.close()

@pytest.mark.parametrize('text',['DECIMAL(0,0)','DECIMAL(39,2)','DECIMAL(2,3)','DECIMAL(x,2)','DECIMAL(1)','UNKNOWN'])
def test_invalid_catalog_type(text):
    storage=MemoryStorage()
    catalog=PersistentCatalog(storage);catalog.bootstrap()
    storage.insert(SYSTEM_CATALOG,(1,'bad',0,'x',text))
    with pytest.raises(MiniSQLError,match='CORRUPT_CATALOG'):catalog.bootstrap()

@pytest.mark.parametrize('column',[C('x',D.DECIMAL,0,0),C('x',D.DECIMAL,10,None),C('x',D.DECIMAL,True,0),C('x',D.INT,10,2)])
def test_invalid_schema_before_catalog_writes(column):
    storage=MemoryStorage()
    catalog=PersistentCatalog(storage);catalog.bootstrap()
    with pytest.raises(MiniSQLError,match='INVALID_SCHEMA'):
        catalog.register_table(T('bad',(C('id',D.INT),column),1))
    assert tuple(storage.scan(SYSTEM_CATALOG))==()
    assert catalog.get_table('bad') is None
