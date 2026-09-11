"""SQL 扩展阶段牵头契约的契约测试：钉死对象目录、依赖保护与鉴权接口面的语义。

同一套契约对内存替身与持久化实现并行验证——持久化实现（engine/objects.py）
必须全部通过才能作为成员二存储验证依赖的正式接口。"""
import pytest

from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, TableSchema
from minisql.engine.auth import AccountStore, create_account
from minisql.engine.catalog import PersistentCatalog
from minisql.engine.objects import (
    ConstraintDefinition, PersistentAccountStore, PersistentObjectCatalog,
)
from tests.fakes.extension import (
    DependencyTracker, IndexDefinition, MemoryObjectCatalog, TriggerDefinition, ViewDefinition,
)
from tests.fakes.memory import MemoryStorage

ITERATIONS = 1000
SCHEMAS = {"t": ("id", "name")}


def memory_objects():
    return MemoryObjectCatalog(SCHEMAS)


def persistent_objects():
    storage = MemoryStorage()
    catalog = PersistentCatalog(storage)
    catalog.bootstrap()
    table = storage.create_table(TableSchema("t", (
        ColumnSchema("id", DataType.INT), ColumnSchema("name", DataType.VARCHAR))))
    catalog.register_table(table)
    objects = PersistentObjectCatalog(storage, catalog)
    objects.bootstrap()
    return objects


def memory_dependencies():
    return DependencyTracker()


def persistent_dependencies():
    storage = MemoryStorage()
    catalog = PersistentCatalog(storage)
    catalog.bootstrap()
    objects = PersistentObjectCatalog(storage, catalog)
    objects.bootstrap()
    return objects


def memory_accounts():
    return AccountStore()


def persistent_accounts():
    storage = MemoryStorage()
    catalog = PersistentCatalog(storage)
    catalog.bootstrap()
    accounts = PersistentAccountStore(storage, catalog)
    accounts.bootstrap()
    return accounts


@pytest.fixture(params=[memory_objects, persistent_objects], ids=["memory", "persistent"])
def object_catalog(request):
    return request.param()


@pytest.fixture(params=[memory_dependencies, persistent_dependencies], ids=["memory", "persistent"])
def dependency_tracker(request):
    return request.param()


@pytest.fixture(params=[memory_accounts, persistent_accounts], ids=["memory", "persistent"])
def account_store(request):
    return request.param()


def test_view_registration_and_lookup_contract(object_catalog):
    view = ViewDefinition("v1", "SELECT id FROM t;", (ColumnSchema("id", DataType.INT),))
    object_catalog.register_view(view)
    assert object_catalog.get_view("V1") == view  # 大小写不敏感
    assert object_catalog.list_views() == (view,)
    with pytest.raises(MiniSQLError) as error:
        object_catalog.register_view(view)
    assert error.value.code == "DUPLICATE_OBJECT"
    object_catalog.unregister_view("v1")
    assert object_catalog.get_view("v1") is None
    with pytest.raises(MiniSQLError) as error:
        object_catalog.unregister_view("v1")
    assert error.value.code == "UNKNOWN_OBJECT"


def test_trigger_ordering_and_event_filter_contract(object_catalog):
    object_catalog.register_trigger(TriggerDefinition(
        "t2", "orders", "INSERT", "SELECT 1;", "AFTER", "2026-09-11T10:00:02"))
    object_catalog.register_trigger(TriggerDefinition(
        "t1", "orders", "INSERT", "SELECT 1;", "AFTER", "2026-09-11T10:00:01"))
    object_catalog.register_trigger(TriggerDefinition(
        "t3", "orders", "DELETE", "SELECT 1;", "AFTER", "2026-09-11T10:00:03"))
    inserts = object_catalog.get_triggers("ORDERS", "insert")
    assert tuple(t.name for t in inserts) == ("t1", "t2")  # 同事件按创建时间先后
    assert tuple(t.name for t in object_catalog.get_triggers("orders", "DELETE")) == ("t3",)


def test_index_listing_contract(object_catalog):
    object_catalog.register_index(IndexDefinition("i1", "t", ("id",), unique=True, root_page=5))
    object_catalog.register_index(IndexDefinition("i2", "t", ("name", "id"), root_page=7))
    assert object_catalog.get_index("I1").unique is True
    assert object_catalog.get_index("i1").root_page == 5
    assert tuple(index.name for index in object_catalog.get_indexes("t")) == ("i1", "i2")
    assert object_catalog.get_indexes("other") == ()


def test_index_requires_root_page_contract(object_catalog):
    """成员二约定：root_page 不持久化则无法重开构造 B+ 树，登记时必填。"""
    with pytest.raises(MiniSQLError) as error:
        object_catalog.register_index(IndexDefinition("i1", "t", ("id",)))
    assert error.value.code == "INVALID_RECORD"


def test_constraint_registration_contract(object_catalog):
    object_catalog.register_constraint(ConstraintDefinition(
        "t", "pk_t", "PRIMARY KEY", ("id",)))
    object_catalog.register_constraint(ConstraintDefinition(
        "t", "ck_t", "CHECK", ("name",), expression="name <> ''"))
    constraints = object_catalog.get_constraints("T")
    assert tuple(c.name for c in constraints) == ("ck_t", "pk_t")  # 按约束名排序
    assert constraints[1].columns == ("id",)
    assert constraints[0].expression == "name <> ''"
    with pytest.raises(MiniSQLError) as error:
        object_catalog.register_constraint(ConstraintDefinition("t", "pk_t", "UNIQUE", ("name",)))
    assert error.value.code == "DUPLICATE_OBJECT"
    with pytest.raises(MiniSQLError) as error:
        object_catalog.register_constraint(ConstraintDefinition("t", "bad", "FLY", ("id",)))
    assert error.value.code == "UNKNOWN_CONSTRAINT_KIND"
    object_catalog.unregister_constraints("t", "ck_t")
    assert tuple(c.name for c in object_catalog.get_constraints("t")) == ("pk_t",)
    object_catalog.unregister_constraints("t")
    assert object_catalog.get_constraints("t") == ()


def test_constraint_rejects_unknown_table_and_column(object_catalog):
    with pytest.raises(MiniSQLError) as error:
        object_catalog.register_constraint(ConstraintDefinition("missing", "c1", "UNIQUE", ("id",)))
    assert error.value.code == "UNKNOWN_TABLE"
    with pytest.raises(MiniSQLError) as error:
        object_catalog.register_constraint(ConstraintDefinition("t", "c1", "UNIQUE", ("missing",)))
    assert error.value.code == "UNKNOWN_COLUMN"


def test_dependency_blocks_drop_until_dependents_removed(dependency_tracker):
    dependency_tracker.add_dependency("view", "v1", "table", "t")
    dependency_tracker.assert_droppable("table", "other")
    with pytest.raises(MiniSQLError) as error:
        dependency_tracker.assert_droppable("table", "t")
    assert error.value.code == "DEPENDENT_OBJECT"
    dependency_tracker.remove_object("view", "v1")  # 删除依赖者
    dependency_tracker.assert_droppable("table", "t")


def test_nested_view_chain_contract(dependency_tracker):
    dependency_tracker.add_dependency("view", "v1", "table", "t")
    dependency_tracker.add_dependency("view", "v2", "view", "v1")
    with pytest.raises(MiniSQLError):
        dependency_tracker.assert_droppable("table", "t")  # v2→v1→t 链上仍被依赖
    dependency_tracker.remove_object("view", "v2")
    with pytest.raises(MiniSQLError):
        dependency_tracker.assert_droppable("table", "t")  # v1 仍在
    dependency_tracker.remove_object("view", "v1")
    dependency_tracker.assert_droppable("table", "t")
    assert dependency_tracker.dependencies("view", "v1") == ()


def test_auth_provider_surface_contract(account_store):
    """钉死 AuthProvider 提议接口面：实现必须提供这些方法。"""
    required = (
        "authenticate", "require", "require_admin",
        "grant", "revoke", "add", "remove_account",
    )
    for name in required:
        assert callable(getattr(account_store, name)), f"缺少接口方法 {name}"
    account_store.add(create_account("root", "pw", is_admin=True, iterations=ITERATIONS))
    session = account_store.authenticate("root", "pw")
    account_store.require_admin(session)
    with pytest.raises(MiniSQLError) as error:
        account_store.require(None, "SELECT", "table", "t")
    assert error.value.code == "PERMISSION_DENIED"
    assert error.value.stage is ErrorStage.EXECUTION


def test_auth_grant_revoke_contract(account_store):
    account_store.add(create_account("alice", "pw", iterations=ITERATIONS))
    session = account_store.authenticate("alice", "pw")
    account_store.grant("alice", "SELECT", "table", "t")
    account_store.require(session, "SELECT", "table", "t")
    account_store.revoke("alice", "SELECT", "table", "t")
    with pytest.raises(MiniSQLError):
        account_store.require(session, "SELECT", "table", "t")  # 撤权立即生效
