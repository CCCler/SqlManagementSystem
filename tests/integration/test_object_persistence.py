"""对象系统表与账户存储的真实文件持久化验收。

对象定义、依赖与账户授权全部通过真实页存储写入系统表，关闭重开后完整恢复；
这是成员二存储侧验证所依赖的正式接口证据。"""
import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType
from minisql.engine.database import open_database
from minisql.engine.objects import IndexDefinition, TriggerDefinition, ViewDefinition


def test_object_tables_roundtrip_real_storage(tmp_path):
    path = tmp_path / "db"
    database = open_database(path)
    database.objects.register_view(
        ViewDefinition("v1", "SELECT id FROM t;", (ColumnSchema("id", DataType.INT),)))
    database.objects.register_trigger(
        TriggerDefinition("tr1", "t", "INSERT", "SELECT 1;", created_order=1))
    database.objects.register_index(IndexDefinition("i1", "t", ("id",), unique=True))
    database.objects.add_dependency("view", "v1", "table", "t")
    database.accounts.create_account("root", "pw", is_admin=True, iterations=1000)
    database.accounts.grant("root", "SELECT", "table", "t")
    database.execute("CREATE TABLE t(id INT);")
    database.close()

    reopened = open_database(path)
    try:
        view = reopened.objects.get_view("V1")
        assert view == ViewDefinition("v1", "SELECT id FROM t;", (ColumnSchema("id", DataType.INT),))
        assert reopened.objects.get_triggers("t", "INSERT")[0].name == "tr1"
        assert reopened.objects.get_index("I1").unique is True
        with pytest.raises(MiniSQLError) as error:
            reopened.objects.assert_droppable("table", "t")
        assert error.value.code == "DEPENDENT_OBJECT"
        session = reopened.accounts.authenticate("root", "pw")
        assert session is not None
        reopened.accounts.require(session, "SELECT", "table", "t")
        # 用户表与对象系统表共存，公开目录不暴露系统表
        assert [table.name for table in reopened.catalog.list_tables()] == ["t"]
        assert reopened.execute("SELECT * FROM t;")[0].rows == ()
    finally:
        reopened.close()


def test_object_unregister_persists_across_restart(tmp_path):
    path = tmp_path / "db"
    column = (ColumnSchema("id", DataType.INT),)
    database = open_database(path)
    database.objects.register_view(ViewDefinition("v1", "SELECT 1;", column))
    database.objects.register_view(ViewDefinition("v2", "SELECT 1;", column))
    database.objects.unregister_view("v1")
    database.close()

    reopened = open_database(path)
    try:
        assert reopened.objects.get_view("v1") is None
        assert reopened.objects.get_view("v2") is not None
    finally:
        reopened.close()


def test_account_removal_and_revocation_persist(tmp_path):
    path = tmp_path / "db"
    database = open_database(path)
    database.accounts.create_account("root", "rootpw", is_admin=True, iterations=1000)
    database.accounts.create_account("alice", "alicepw", iterations=1000)
    database.accounts.grant("alice", "SELECT", "table", "t")
    database.accounts.grant("alice", "INSERT", "table", "t")
    database.accounts.revoke("alice", "INSERT", "table", "t")
    database.accounts.remove_account(  # 仅需满足管理员检查；root 是唯一管理员不能删
        database.accounts.authenticate("root", "rootpw"), "alice")
    database.close()

    reopened = open_database(path)
    try:
        assert reopened.accounts.authenticate("alice", "alicepw") is None  # 已删除
        assert reopened.accounts.authenticate("root", "rootpw") is not None
    finally:
        reopened.close()


def test_old_database_coexists_without_id_collision(tmp_path):
    """既有库（用户表已占编号）升级后对象系统表动态排后，无需迁移。"""
    from minisql.engine.catalog import PersistentCatalog
    from minisql.engine.objects import PersistentObjectCatalog
    from minisql.storage.buffer import PageBufferPool
    from minisql.storage.file_manager import FileManager
    from minisql.storage.page import DiskPageManager
    from minisql.storage.record import HeapStorage

    path = tmp_path / "old.db"
    files = FileManager(path)
    pages = DiskPageManager(files)
    buffer = PageBufferPool(pages)
    storage = HeapStorage(pages, buffer)
    catalog = PersistentCatalog(storage)
    catalog.bootstrap()
    from minisql.contracts.models import TableSchema
    user_table = storage.create_table(TableSchema("t", (ColumnSchema("id", DataType.INT),)))
    catalog.register_table(user_table)
    assert user_table.table_id == 1  # 旧库用户表已占 1 号
    objects = PersistentObjectCatalog(storage, catalog)
    objects.bootstrap()
    # 对象系统表在用户表之后动态分配，不冲突；用户表可正常读写。
    objects.register_view(ViewDefinition("v1", "SELECT 1;", (ColumnSchema("id", DataType.INT),)))
    assert catalog.get_table("t") == user_table
    assert objects.get_view("v1") is not None
    storage.close()
