# SQL 扩展阶段：成员三牵头契约的接口示例（供三人评审）

按协作规则"开发前形成接口示例与契约测试，三人共同评审"，本文给出成员三牵头
四类契约的接口签名与用法示例。内存替身见 `tests/fakes/extension.py`，契约测试
见 `tests/contracts/test_extension_contracts.py`——评审通过后由成员三把正式类型
移入 `contracts/`，真实持久化实现落 `engine/`，内存替身保留为隔离夹具。

## 一、对象目录：ObjectCatalog

视图/触发器/索引的定义管理，持久化实现按契约提案的 `__views/__triggers/
__indexes` 系统表落地；名称统一小写键。

```python
class ObjectCatalog(Protocol):
    # 视图
    def register_view(self, view: ViewDefinition) -> None: ...
    def get_view(self, name: str) -> ViewDefinition | None: ...
    def list_views(self) -> tuple[ViewDefinition, ...]: ...
    def unregister_view(self, name: str) -> None: ...
    # 触发器（get_triggers 按表+事件过滤，按 created_order 升序）
    def register_trigger(self, trigger: TriggerDefinition) -> None: ...
    def get_trigger(self, name: str) -> TriggerDefinition | None: ...
    def get_triggers(self, table: str, event: str) -> tuple[TriggerDefinition, ...]: ...
    def unregister_trigger(self, name: str) -> None: ...
    # 索引
    def register_index(self, index: IndexDefinition) -> None: ...
    def get_index(self, name: str) -> IndexDefinition | None: ...
    def get_indexes(self, table: str) -> tuple[IndexDefinition, ...]: ...
    def unregister_index(self, name: str) -> None: ...
```

语义约定（契约测试已钉死）：

- 重名注册报 `DUPLICATE_OBJECT`（SEMANTIC），注销不存在报 `UNKNOWN_OBJECT`；
- 触发器同表同事件多个时按创建先后执行；
- `ViewDefinition` 保存规范化 SQL + 编译后的输出列清单（重开时恢复目录项，
  查询时再编译展开，与成员一编译行为一致）。

**成员一用法**：编译 CREATE VIEW 时调用 `get_view` 查重、生成 `ViewDefinition`
（含输出列）；DROP VIEW 前调用 `get_view` 确认存在。
**成员二用法**：按 `__views/__triggers/__indexes` 列结构提供记录接口即可，
对象语义由成员三负责。

## 二、依赖保护：DependencyTracker

```python
class DependencyTracker(Protocol):
    def add_dependency(self, object_type: str, object_name: str,
                       depends_on_type: str, depends_on_name: str) -> None: ...
    def remove_object(self, object_type: str, object_name: str) -> None: ...
    def dependencies(self, object_type: str, object_name: str) -> tuple[tuple[str, str], ...]: ...
    def dependents(self, object_type: str, object_name: str) -> tuple[tuple[str, str], ...]: ...
    def assert_droppable(self, object_type: str, object_name: str) -> None: ...
```

语义约定：DROP/ALTER 前调用 `assert_droppable`，存在依赖者报 `DEPENDENT_OBJECT`
（SEMANTIC）并拒绝操作；首版不隐式级联。嵌套链（v2→v1→t）在 v1 删除前
t 始终不可删（契约测试覆盖）。

**成员一用法**：编译 CREATE VIEW 时返回依赖清单，由成员三在成功执行后登记。
**成员三用法**：DROP TABLE/VIEW、ALTER 前统一走 `assert_droppable`。

## 三、会话与鉴权：AuthProvider

已有引擎原型 `minisql/engine/auth.py`，公共接口面如下（契约测试钉死方法面）：

```python
class AuthProvider(Protocol):
    def authenticate(self, name: str, password: str) -> Session | None: ...
    def require(self, session: Session | None, permission: str,
                object_kind: str | None = None, object_name: str | None = None) -> None: ...
    def require_admin(self, session: Session | None) -> None: ...
    def grant(self, user: str, permission: str, object_kind: str, object_name: str) -> None: ...
    def revoke(self, user: str, permission: str, object_kind: str, object_name: str) -> None: ...
    def remove_account(self, admin: Session | None, name: str) -> None: ...
```

语义约定：管理员全通过；普通用户实时对照授权表（撤权立即生效）；未登录报
`PERMISSION_DENIED`（EXECUTION）；同名重建账户分配新 `account_id`，旧会话
不可继承新身份权限；最后一个管理员不可删除。持久化实现替换 `AccountStore`
的内存表为 `__users/__grants` 系统表。

**成员一用法**：F12 语法编译不接触密码；运行时身份由成员三在 Database 统一
入口注入（视图/子查询/触发器动作沿执行上下文继承调用者身份）。

## 四、结果序列化

展示层契约已在 `cli/main.py` 的 `format_value` 与 `cli/trace.py` 的
`to_json_value` 落地（NULL→`NULL`、BOOL→TRUE/FALSE、日期时间→ISO、
DECIMAL→字符串），对应测试见 `tests/integration/test_display.py`。
待成员二类型契约定稿后同步 `ExecutionResult` 的值域声明。

## 五、评审后迁移清单（成员三执行）

1. `ViewDefinition/TriggerDefinition/IndexDefinition` 移入 `contracts/models.py`；
2. `ObjectCatalog/DependencyTracker/AuthProvider` Protocol 移入 `contracts/interfaces.py`；
3. ~~`engine/` 实现持久化版本~~ **已落地**：`minisql/engine/objects.py` 提供
   `PersistentObjectCatalog`（视图/触发器/索引/依赖）与 `PersistentAccountStore`
   （账户/授权），六张系统表动态编号并登记进 `__catalog`；数据库生命周期
   （`TransactionalDatabase._reload`）自动 bootstrap，`database.objects` /
   `database.accounts` 直接可用，契约测试对内存替身与持久化实现并行验证；
4. 更新 `docs/接口约定.md` 与 `docs/SQL扩展契约提案-成员三.md` 的对应条目。
