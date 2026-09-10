"""SQL 扩展阶段牵头契约的契约测试：钉死对象目录、依赖保护与鉴权接口面的语义。

实现方（engine 持久化版本）必须让本文件全部通过才能替换内存替身。"""
import pytest

from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType
from minisql.engine.auth import AccountStore, create_account
from tests.fakes.extension import (
    DependencyTracker, IndexDefinition, MemoryObjectCatalog, TriggerDefinition, ViewDefinition,
)

ITERATIONS = 1000


def test_view_registration_and_lookup_contract():
    catalog = MemoryObjectCatalog()
    view = ViewDefinition("v1", "SELECT id FROM t;", (ColumnSchema("id", DataType.INT),))
    catalog.register_view(view)
    assert catalog.get_view("V1") == view  # 大小写不敏感
    assert catalog.list_views() == (view,)
    with pytest.raises(MiniSQLError) as error:
        catalog.register_view(view)
    assert error.value.code == "DUPLICATE_OBJECT"
    catalog.unregister_view("v1")
    assert catalog.get_view("v1") is None
    with pytest.raises(MiniSQLError) as error:
        catalog.unregister_view("v1")
    assert error.value.code == "UNKNOWN_OBJECT"


def test_trigger_ordering_and_event_filter_contract():
    catalog = MemoryObjectCatalog()
    catalog.register_trigger(TriggerDefinition("t2", "orders", "INSERT", "SELECT 1;", created_order=2))
    catalog.register_trigger(TriggerDefinition("t1", "orders", "INSERT", "SELECT 1;", created_order=1))
    catalog.register_trigger(TriggerDefinition("t3", "orders", "DELETE", "SELECT 1;", created_order=0))
    inserts = catalog.get_triggers("ORDERS", "insert")
    assert tuple(t.name for t in inserts) == ("t1", "t2")  # 同事件按创建先后
    assert tuple(t.name for t in catalog.get_triggers("orders", "DELETE")) == ("t3",)


def test_index_listing_contract():
    catalog = MemoryObjectCatalog()
    catalog.register_index(IndexDefinition("i1", "t", ("id",), unique=True))
    catalog.register_index(IndexDefinition("i2", "t", ("name", "id")))
    assert catalog.get_index("I1").unique is True
    assert tuple(index.name for index in catalog.get_indexes("t")) == ("i1", "i2")
    assert catalog.get_indexes("other") == ()


def test_dependency_blocks_drop_until_dependents_removed():
    tracker = DependencyTracker()
    tracker.add_dependency("view", "v1", "table", "t")
    tracker.assert_droppable("table", "other")
    with pytest.raises(MiniSQLError) as error:
        tracker.assert_droppable("table", "t")
    assert error.value.code == "DEPENDENT_OBJECT"
    tracker.remove_object("view", "v1")  # 删除依赖者
    tracker.assert_droppable("table", "t")


def test_nested_view_chain_contract():
    tracker = DependencyTracker()
    tracker.add_dependency("view", "v1", "table", "t")
    tracker.add_dependency("view", "v2", "view", "v1")
    with pytest.raises(MiniSQLError):
        tracker.assert_droppable("table", "t")  # v2→v1→t 链上仍被依赖
    tracker.remove_object("view", "v2")
    with pytest.raises(MiniSQLError):
        tracker.assert_droppable("table", "t")  # v1 仍在
    tracker.remove_object("view", "v1")
    tracker.assert_droppable("table", "t")
    assert tracker.dependencies("view", "v1") == ()


def test_auth_provider_surface_contract():
    """钉死 AuthProvider 提议接口面：AccountStore 必须提供这些方法。"""
    required = (
        "authenticate", "require", "require_admin",
        "grant", "revoke", "add", "remove_account",
    )
    for name in required:
        assert callable(getattr(AccountStore, name)), f"AccountStore 缺少接口方法 {name}"
    # 行为抽查（细节由 tests/engine/test_auth.py 覆盖）
    store = AccountStore()
    store.add(create_account("root", "pw", is_admin=True, iterations=ITERATIONS))
    session = store.authenticate("root", "pw")
    store.require_admin(session)
    with pytest.raises(MiniSQLError) as error:
        store.require(None, "SELECT", "table", "t")
    assert error.value.code == "PERMISSION_DENIED"
    assert error.value.stage is ErrorStage.EXECUTION
