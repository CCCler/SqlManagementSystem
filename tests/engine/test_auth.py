"""F12 鉴权模型原型验收：散列、会话、授权、撤权与管理员保护。"""
import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.engine.auth import (
    PBKDF2_ITERATIONS, AccountStore, create_account, verify_password,
)

ITERATIONS = 1000  # 测试用小迭代次数控制耗时；常量下限单独断言。


def test_password_roundtrip_and_rejection():
    account = create_account("alice", "s3cret!", iterations=ITERATIONS)
    assert verify_password("s3cret!", account.salt, account.key, ITERATIONS)
    assert not verify_password("wrong", account.salt, account.key, ITERATIONS)
    assert not verify_password("s3cret!", account.salt, b"\x00" * 32, ITERATIONS)


def test_salt_is_unique_per_account():
    first = create_account("a", "same-password", iterations=ITERATIONS)
    second = create_account("b", "same-password", iterations=ITERATIONS)
    assert first.salt != second.salt
    assert first.key != second.key


def test_account_never_stores_plaintext():
    account = create_account("alice", "plaintext-password", iterations=ITERATIONS)
    for value in (account.name, account.salt.hex(), account.key.hex(), str(account.is_admin)):
        assert "plaintext-password" not in value


def test_default_iteration_constant_is_high():
    assert PBKDF2_ITERATIONS >= 100_000


def test_authenticate_and_authorization_flow():
    store = AccountStore()
    store.add(create_account("root", "adminpw", is_admin=True, iterations=ITERATIONS))
    store.add(create_account("alice", "pw", iterations=ITERATIONS))

    session = store.authenticate("alice", "pw")
    assert session is not None and session.account == "alice"
    assert store.authenticate("alice", "bad") is None
    assert store.authenticate("nobody", "pw") is None

    with pytest.raises(MiniSQLError) as error:
        store.require(None, "SELECT", "table", "t")  # 未登录
    assert error.value.code == "PERMISSION_DENIED"
    with pytest.raises(MiniSQLError):
        store.require(session, "SELECT", "table", "t")  # 未授权

    store.grant("alice", "SELECT", "table", "t")
    store.require(session, "SELECT", "table", "t")
    store.revoke("alice", "SELECT", "table", "t")
    with pytest.raises(MiniSQLError):
        store.require(session, "SELECT", "table", "t")  # 撤权立即生效


def test_admin_has_full_access_and_last_admin_protected():
    store = AccountStore()
    store.add(create_account("root", "adminpw", is_admin=True, iterations=ITERATIONS))
    admin_session = store.authenticate("root", "adminpw")
    store.require(admin_session, "DROP", "table", "t")  # 管理员无需逐项授权
    store.require_admin(admin_session)

    with pytest.raises(MiniSQLError) as error:
        store.remove_account(admin_session, "root")
    assert error.value.code == "PERMISSION_DENIED"

    with pytest.raises(MiniSQLError):
        store.require_admin(None)


def test_unknown_user_and_unknown_permission():
    store = AccountStore()
    with pytest.raises(MiniSQLError) as error:
        store.grant("missing", "SELECT", "table", "t")
    assert error.value.code == "UNKNOWN_USER"
    store.add(create_account("alice", "pw", iterations=ITERATIONS))
    with pytest.raises(MiniSQLError) as error:
        store.grant("alice", "FLY", "table", "t")
    assert error.value.code == "UNKNOWN_PERMISSION"
    with pytest.raises(MiniSQLError) as error:
        store.grant("alice", "SELECT", "role", "t")
    assert error.value.code == "UNKNOWN_OBJECT_KIND"


@pytest.mark.parametrize("new_admin", [False, True])
def test_deleted_account_session_cannot_access_recreated_account(new_admin):
    store = AccountStore()
    store.add(create_account("root", "rootpw", is_admin=True, iterations=ITERATIONS))
    store.add(create_account("alice", "oldpw", iterations=ITERATIONS))
    admin = store.authenticate("root", "rootpw")
    old = store.authenticate("alice", "oldpw")
    store.grant("alice", "SELECT", "table", "t")
    store.remove_account(admin, "ALICE")
    with pytest.raises(MiniSQLError, match="PERMISSION_DENIED"):
        store.require(old, "SELECT", "table", "t")
    store.add(create_account("ALICE", "newpw", is_admin=new_admin, iterations=ITERATIONS))
    store.grant("alice", "SELECT", "table", "t")
    fresh = store.authenticate("alice", "newpw")
    assert fresh.account_id != old.account_id
    assert store.authenticate("alice", "oldpw") is None
    store.require(fresh, "SELECT", "table", "t")
    with pytest.raises(MiniSQLError, match="PERMISSION_DENIED"):
        store.require(old, "SELECT", "table", "t")
    with pytest.raises(MiniSQLError, match="PERMISSION_DENIED"):
        store.require_admin(old)
    with pytest.raises(MiniSQLError, match="PERMISSION_DENIED"):
        store.remove_account(old, "root")
    if new_admin:
        store.require_admin(fresh)


def test_session_cannot_cross_stores_with_same_account_name():
    first, second = AccountStore(), AccountStore()
    for store in (first, second):
        store.add(create_account("root", "pw", is_admin=True, iterations=ITERATIONS))
    session = first.authenticate("root", "pw")
    first.require_admin(session)
    with pytest.raises(MiniSQLError, match="PERMISSION_DENIED"):
        second.require_admin(session)


@pytest.mark.parametrize("iterations", [1000, 2000, PBKDF2_ITERATIONS])
def test_authentication_uses_stored_parameters(iterations):
    from dataclasses import asdict
    from minisql.engine.auth import Account
    account = create_account("alice", "pw", iterations=iterations)
    # 模拟账户字段保存后重新加载，身份和派生次数必须一并保留。
    restored = Account(**asdict(account))
    store = AccountStore()
    store.add(restored)
    assert restored.iterations == iterations
    session = store.authenticate("ALICE", "pw")
    assert session is not None and session.account_id == account.account_id
    assert store.authenticate("alice", "wrong") is None


def test_index_authorization_accepts_drop_only():
    store = AccountStore()
    store.add(create_account("alice", "pw", iterations=ITERATIONS))
    store.grant("alice", "DROP", "index", "idx")
    store.require(store.authenticate("alice", "pw"), "DROP", "index", "idx")
    with pytest.raises(MiniSQLError, match="UNKNOWN_PERMISSION"):
        store.grant("alice", "SELECT", "index", "idx")
