"""对象系统表与账户存储的真实文件持久化验收。

对象定义、约束、依赖与账户授权全部通过真实页存储写入系统表，关闭重开后
完整恢复；这是成员二存储侧验证所依赖的正式接口证据。"""
import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType
from minisql.engine.database import open_database
from minisql.engine.objects import (
    ConstraintDefinition, IndexDefinition, TriggerDefinition, ViewDefinition,
)


def _serialized_check() -> str:
    """构造 name <> '' 的序列化 CHECK 表达式（列序号 1）。"""
    from minisql.contracts.extensions import Expr, FieldBinding, TypeSpec
    from minisql.contracts.models import SourcePosition
    from minisql.engine.expr import serialize_expr
    binding = FieldBinding(0, 0, 1, "", "name", TypeSpec("VARCHAR"))
    return serialize_expr(Expr("<>", (
        Expr("column", (None, "name"), SourcePosition(1, 1), None, binding),
        Expr("literal", ("",), SourcePosition(1, 1)))))


def test_object_tables_roundtrip_real_storage(tmp_path):
    path = tmp_path / "db"
    database = open_database(path)
    database.execute("CREATE TABLE t(id INT, name VARCHAR);")
    database.objects.register_view(
        ViewDefinition("v1", "SELECT id FROM t;", (ColumnSchema("id", DataType.INT),)))
    database.objects.register_trigger(
        TriggerDefinition("tr1", "t", "INSERT", "SELECT 1;", 1))
    database.objects.register_index(IndexDefinition("i1", "t", ("id",), unique=True, root_page=5))
    database.objects.register_constraint(ConstraintDefinition("t", "pk_t", "PRIMARY KEY", ("id",)))
    database.objects.register_constraint(ConstraintDefinition(
        "t", "ck_t", "CHECK", ("name",), expression=_serialized_check()))
    database.objects.add_dependency("view", "v1", "table", "t")
    database.accounts.create_account("root", "pw", is_admin=True, iterations=1000)
    database.accounts.grant("root", "SELECT", "table", "t")
    database.close()

    reopened = open_database(path)
    try:
        view = reopened.objects.get_view("V1")
        assert view == ViewDefinition("v1", "SELECT id FROM t;", (ColumnSchema("id", DataType.INT),))
        assert reopened.objects.get_triggers("t", "INSERT")[0].name == "tr1"
        index = reopened.objects.get_index("I1")
        assert index.unique is True and index.root_page == 5  # root_page 持久化供重开构造 B+ 树
        constraints = reopened.objects.get_constraints("t")
        assert tuple((c.name, c.kind, c.columns) for c in constraints) == (
            ("ck_t", "CHECK", ("name",)), ("pk_t", "PRIMARY KEY", ("id",)))
        assert constraints[0].expression == _serialized_check()
        with pytest.raises(MiniSQLError) as error:
            reopened.objects.assert_droppable("table", "t")
        assert error.value.code == "DEPENDENT_OBJECT"
        session = reopened.accounts.authenticate("root", "pw")
        assert session is not None
        reopened.accounts.require(session, "SELECT", "table", "t")
        assert reopened.login("root", "pw")  # 已有账户：SQL 需登录后执行
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


def test_constraint_unregister_persists_across_restart(tmp_path):
    path = tmp_path / "db"
    database = open_database(path)
    database.execute("CREATE TABLE t(id INT, name VARCHAR);")
    database.objects.register_constraint(ConstraintDefinition("t", "nn_t", "NOT NULL", ("name",)))
    database.objects.register_constraint(ConstraintDefinition("t", "pk_t", "PRIMARY KEY", ("id",)))
    database.objects.unregister_constraints("t", "pk_t")
    database.close()

    reopened = open_database(path)
    try:
        constraints = reopened.objects.get_constraints("t")
        assert tuple(c.name for c in constraints) == ("nn_t",)
        assert constraints[0].kind == "NOT NULL" and constraints[0].columns == ("name",)
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
    from minisql.contracts.models import TableSchema

    path = tmp_path / "old.db"
    files = FileManager(path)
    pages = DiskPageManager(files)
    buffer = PageBufferPool(pages)
    storage = HeapStorage(pages, buffer)
    catalog = PersistentCatalog(storage)
    catalog.bootstrap()
    user_table = storage.create_table(TableSchema("t", (ColumnSchema("id", DataType.INT),)))
    catalog.register_table(user_table)
    assert user_table.table_id == 1  # 旧库用户表已占 1 号
    objects = PersistentObjectCatalog(storage, catalog)
    objects.bootstrap()
    # 对象系统表在用户表之后动态分配，不冲突；用户表可正常读写。
    objects.register_view(ViewDefinition("v1", "SELECT 1;", (ColumnSchema("id", DataType.INT),)))
    objects.register_index(IndexDefinition("i1", "t", ("id",), root_page=9))
    assert catalog.get_table("t") == user_table
    assert objects.get_view("v1") is not None
    assert objects.get_index("i1").root_page == 9
    storage.close()
