# SQL 扩展阶段：成员三牵头契约提案与待确认事项方案

本文是 [SQL 扩展三人分工](SQL扩展三人分工.md) 阶段 1 的成员三交付：牵头
Schema/Catalog、依赖、会话与结果四类契约的提案，以及五类"实施前待确认事项"
的建议默认方案。所有内容待三人评审与用户确认，未确认前不修改公共代码。

## 一、Schema 与 Catalog 扩展

### 1.1 新增系统表（建议：固定保留编号段）

现状：table_id=0 保留给 `__catalog`，用户表从 1 开始。新增对象类型需要持久化，
建议为系统表固定保留编号段 1..6，用户表编号从 7 开始：

| 系统表 | 建议编号 | 列（固定结构） |
|---|---|---|
| `__views` | 1 | view_id INT, view_name VARCHAR, definition VARCHAR, column_index INT, column_name VARCHAR, column_type VARCHAR |
| `__triggers` | 2 | trigger_id INT, trigger_name VARCHAR, table_name VARCHAR, event VARCHAR, timing VARCHAR, action VARCHAR, created_at VARCHAR |
| `__indexes` | 3 | index_id INT, index_name VARCHAR, table_name VARCHAR, unique_flag INT, column_index INT, column_name VARCHAR |
| `__users` | 4 | user_id INT, account_id VARCHAR, user_name VARCHAR, salt VARCHAR, key VARCHAR, iterations INT, is_admin INT |
| `__grants` | 5 | user_name VARCHAR, object_type VARCHAR, object_name VARCHAR, permission VARCHAR |
| `__dependencies` | 6 | object_type VARCHAR, object_name VARCHAR, depends_on_type VARCHAR, depends_on_name VARCHAR |

- 沿用 `__catalog` 的"固定结构、绕过 SQL 编译直接恢复"模式；表名统一小写键。
- **备选方案 B**：不固定编号，bootstrap 时按固定顺序动态分配（新建库编号稳定，
  旧库编号由成员二的迁移重写统一处理）。A 的优点是恢复入口固定、无需查目录找编号。
- 旧库兼容：现有库的用户表编号 1..N 与新保留段冲突，须由成员二的格式迁移在
  升级时重映射用户表编号，并同步改写 `__catalog` 行——迁移方案由成员二牵头，
  本提案仅声明"保留段占 1..6、用户表从 7 开始"作为待确认约定。

### 1.2 对象定义与依赖

- 视图/触发器/索引/账户的定义均通过上述系统表持久化，随事务日志一起提交与恢复。
- `__dependencies` 记录视图依赖的表/视图、触发器依赖的表。DROP/ALTER 前由成员三
  查询依赖表，发现依赖对象时报 `DEPENDENT_OBJECT` 并拒绝操作（首版不隐式级联）。
- 视图定义保存"创建时的规范化 SQL 文本 + 编译后的输出列清单"；重开时按固定结构
  恢复目录项，查询时再编译展开（保证与成员一编译行为一致）。

## 二、会话与鉴权模型（F12 引擎侧）

### 2.1 账户与密码

- 密码存储：独立盐 + `hashlib.pbkdf2_hmac("sha256", ...)`（标准库，60 万次迭代，
  每个账户保存实际迭代次数，默认参数只用于新建账户），验证用 `secrets.compare_digest` 防时序侧信道。
- 账户记录不保存明文；密码派生结果不进入查询结果、编译跟踪（--trace）与日志。

### 2.2 会话与统一入口鉴权

- 登录后建立会话身份（账户名 + 不可复用的 account_id）；每次鉴权重新匹配账户身份并读取当前管理员状态。删除后同名重建必须生成新 account_id，旧会话不可恢复权限；未登录时只能执行登录与初始化语句。
- Database 统一入口在每次 execute 前完成鉴权；视图展开、子查询、触发器动作沿
  执行上下文继承调用者身份，权限检查覆盖其访问的底层对象，不因入口不同而绕过。
- 撤权立即生效：每次操作实时对照 `__grants` 检查，不缓存授权结果。

### 2.3 授权粒度（建议默认）

| 权限 | 适用对象 | 说明 |
|---|---|---|
| SELECT / INSERT / UPDATE / DELETE | 表、视图 | 数据操作权限 |
| CREATE TABLE / DROP / ALTER | 表 | DDL 权限 |
| CREATE INDEX / CREATE VIEW / CREATE TRIGGER | 对应对象 | 对象创建权限 |
| GRANT / REVOKE / CREATE USER | 全局 | 仅管理员 |

- 管理员拥有全部权限且不可被撤权；**最后一个管理员不可删除或降级**。
- 视图与触发器按**调用者权限**执行（覆盖其引用的底层对象）。

## 三、结果序列化与展示

- `ExecutionResult.rows` 的值域扩展：NULL（None）、DECIMAL（Decimal）、DATE/TIME/
  TIMESTAMP（date/time/datetime）、BOOL 表列（bool）。契约类型由成员二牵头，
  本提案只约定执行结果与展示层的行为。
- CLI 表格：NULL 显示为 `NULL`；日期时间按 ISO 格式显示；DECIMAL 按声明精度显示。
- `cli/trace.py` 的 `to_json_value` 扩展新类型（Enum→值、Decimal→字符串、日期→
  ISO 字符串），避免 JSON 序列化失败。
- GUI 结果表沿用同一渲染规则；EXPLAIN 展示来自实际编译调用，不重复执行 SQL。

## 四、五项待确认事项的建议方案

| 事项 | 建议默认方案 |
|---|---|
| 首次管理员 | 首次打开新库且系统表不存在时进入初始化模式：本机 CLI 交互设置管理员密码（输入不回显、需二次确认）；初始化完成前拒绝任何其他会话，不生成默认弱密码 |
| 类型转换与 ALTER | 严格模式：变更前先对全量数据做兼容性验证（类型、约束、默认值），全部通过后才重写切换；仅允许明确无损的转换（VARCHAR 加宽、INT→VARCHAR 等），拒绝有歧义或有损转换（如 DECIMAL→INT）；验证失败时旧结构保持完整可用 |
| 外键与依赖 | 首版拒绝一切破坏引用完整性的 UPDATE/DELETE/DDL（报 FOREIGN_KEY_VIOLATION / DEPENDENT_OBJECT），不实现隐式级联 |
| 触发器细节 | AFTER 行级；同一事件多个触发器按创建先后执行；动作仅允许写语句与 SELECT；直接递归（动作触发自身）与间接递归（A→B→A 链）均通过执行栈检测拒绝 |
| 授权粒度 | 见 2.3 表；对象权限按表/视图级，不设列级；DDL 权限独立列出；最后一个管理员保护 |

## 五、未决项（评审时请成员一/二补充意见）

1. 系统表编号：固定保留段（方案 A）还是动态分配（方案 B）；旧库用户表编号重映射
   与成员二迁移方案的关系需要联合确认。
2. UPDATE 已在当前基线实现，支持多列旧值赋值、WHERE、事务及 EXPLAIN UPDATE；后续需在既有路径接入新增约束、索引维护与触发器，不再作为待引入语法。
3. 新类型的显示格式细节（DECIMAL 精度、时区行为）依赖成员二的类型编码定稿。
4. 触发器动作中的 SELECT 结果如何处理（丢弃？报错？）。
5. 登录/初始化语句是否进入事务日志（建议：登录不进入；初始化作为普通事务提交）。

## 六、原型当前接口与接入限制

内存原型 Account 保存 account_id 和 iterations，authenticate(name, password) 自动使用账户保存的密码参数，不接受调用者覆盖次数。恢复账户记录时须保留这两个字段；新建同名账户时必须分配新身份。Session 仅由认证成功的服务端流程创建，不能将客户端提交的账户名或 account_id 直接当作登录凭据。

上述字段已在内存原型验证，系统表结构仍属待评审提案；账户持久化、数据库统一入口与 GUI 登录尚未接入，不代表当前数据库已启用权限控制。
