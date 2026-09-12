"""账户认证与授权模型原型（F12 引擎侧，阶段 1 先行验证）。

密码使用带独立盐的标准库 PBKDF2-HMAC-SHA256 派生，验证用固定时间比较，
账户不保存明文；授权实时对照授权表检查，撤权立即生效，不缓存授权结果；
管理员全权限且最后一个管理员受保护。本模块只含模型与检查逻辑，账户与
授权表的持久化在 __users/__grants 系统表方案评审后接入。"""
import hashlib
import secrets
from dataclasses import dataclass, field
from uuid import uuid4

from minisql.contracts.errors import ErrorStage, MiniSQLError

PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16

# 授权粒度（建议默认，详见 docs/SQL扩展契约提案-成员三.md 2.3）
PERMISSIONS = (
    "SELECT", "INSERT", "UPDATE", "DELETE",
    "CREATE TABLE", "DROP", "ALTER",
    "CREATE INDEX", "CREATE VIEW", "CREATE TRIGGER",
)

OBJECT_KINDS = ("table", "view", "database", "index", "trigger")


def generate_salt() -> bytes:
    return secrets.token_bytes(SALT_BYTES)


def derive_key(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def verify_password(password: str, salt: bytes, expected_key: bytes,
                    iterations: int = PBKDF2_ITERATIONS) -> bool:
    """固定时间比较，避免时序侧信道；派生结果与账户记录中的 key 比对。"""
    return secrets.compare_digest(derive_key(password, salt, iterations), expected_key)


def permission_error(reason: str) -> MiniSQLError:
    return MiniSQLError(ErrorStage.EXECUTION, "PERMISSION_DENIED", reason)


@dataclass(frozen=True)
class Account:
    """账户记录：不保存明文密码；key 为 PBKDF2 派生结果。"""

    name: str
    salt: bytes
    key: bytes
    is_admin: bool = False
    iterations: int = PBKDF2_ITERATIONS
    account_id: str = field(default_factory=lambda: uuid4().hex)


def create_account(name: str, password: str, is_admin: bool = False,
                   iterations: int = PBKDF2_ITERATIONS) -> Account:
    salt = generate_salt()
    return Account(name.lower(), salt, derive_key(password, salt, iterations),
                   is_admin, iterations)


@dataclass(frozen=True)
class Session:
    """登录后的会话身份；授权结果不缓存，每次检查实时对照授权表。"""

    account: str
    account_id: str


class AccountStore:
    """账户与授权表的内存实现；F12 集成时替换为 __users/__grants 系统表。"""

    def __init__(self):
        self.accounts: dict[str, Account] = {}
        self.grants: dict[tuple[str, str, str], set[str]] = {}

    def add(self, account: Account) -> None:
        key = account.name.lower()
        if key in self.accounts:
            raise MiniSQLError(ErrorStage.EXECUTION, "DUPLICATE_USER", key)
        self.accounts[key] = account

    def authenticate(self, name: str, password: str) -> Session | None:
        account = self.accounts.get(name.lower())
        if account is None or not verify_password(password, account.salt, account.key, account.iterations):
            return None
        return Session(account.name, account.account_id)

    def grant(self, user: str, permission: str, object_kind: str, object_name: str) -> None:
        if user.lower() not in self.accounts:
            raise MiniSQLError(ErrorStage.EXECUTION, "UNKNOWN_USER", user)
        if permission not in PERMISSIONS:
            raise MiniSQLError(ErrorStage.EXECUTION, "UNKNOWN_PERMISSION", permission)
        if object_kind not in OBJECT_KINDS:
            raise MiniSQLError(ErrorStage.EXECUTION, "UNKNOWN_OBJECT_KIND", object_kind)
        valid = {
            "database": {"CREATE TABLE", "CREATE INDEX", "CREATE VIEW", "CREATE TRIGGER"},
            "table": {"SELECT", "INSERT", "UPDATE", "DELETE", "DROP", "ALTER"},
            "view": {"SELECT", "DROP"}, "index": {"DROP"}, "trigger": {"DROP"},
        }
        if permission not in valid[object_kind]:
            raise MiniSQLError(ErrorStage.EXECUTION, "UNKNOWN_PERMISSION", "权限与对象不匹配")
        key = (user.lower(), object_kind, object_name.lower())
        self.grants.setdefault(key, set()).add(permission)

    def revoke(self, user: str, permission: str, object_kind: str, object_name: str) -> None:
        key = (user.lower(), object_kind, object_name.lower())
        if key in self.grants:
            self.grants[key].discard(permission)

    def _account(self, session: Session | None) -> Account | None:
        if session is None:
            return None
        account = self.accounts.get(session.account)
        # 同名重建是新身份，不能让旧会话继承其授权或管理员权限。
        if account is None or account.account_id != session.account_id:
            return None
        return account

    def require(self, session: Session | None, permission: str,
                object_kind: str | None = None, object_name: str | None = None) -> None:
        """统一鉴权入口：管理员全通过；普通用户实时对照授权表；未登录拒绝。

        视图展开、子查询与触发器动作沿执行上下文继承调用者身份，均经此入口
        检查其访问的底层对象，保证不存在绕过入口的鉴权路径。
        """
        if session is None:
            raise permission_error("未登录：请先使用账户登录")
        account = self._account(session)
        if account is None:
            raise permission_error("会话账户不存在或已被删除")
        if account.is_admin:
            return
        if object_kind is None or object_name is None:
            raise permission_error(f"账户 {session.account} 缺少权限：{permission}")
        granted = self.grants.get((session.account, object_kind, object_name.lower()), set())
        if permission not in granted:
            raise permission_error(
                f"账户 {session.account} 对 {object_kind} {object_name} 缺少权限：{permission}")

    def require_admin(self, session: Session | None) -> None:
        account = self._account(session)
        if account is None or not account.is_admin:
            raise permission_error("需要管理员权限")

    def admin_count(self) -> int:
        return sum(1 for account in self.accounts.values() if account.is_admin)

    def remove_account(self, admin: Session | None, name: str) -> None:
        """删除账户并回收其全部授权；最后一个管理员不可删除。"""
        self.require_admin(admin)
        key = name.lower()
        account = self.accounts.get(key)
        if account is None:
            raise MiniSQLError(ErrorStage.EXECUTION, "UNKNOWN_USER", name)
        if account.is_admin and self.admin_count() <= 1:
            raise permission_error("不能删除最后一个管理员账户")
        del self.accounts[key]
        for grant_key in [k for k in self.grants if k[0] == key]:
            del self.grants[grant_key]
