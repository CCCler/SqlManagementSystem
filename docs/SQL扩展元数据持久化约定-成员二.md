# SQL 扩展元数据持久化约定（成员二提案）

本文是 [SQL 扩展三人分工](SQL扩展三人分工.md) 中成员二唯一未完成项
「F07/F10/F11/F12：验证约束、视图、触发器及账户权限元数据通过记录接口持久化和恢复」的
前置契约草案：成员三提案的 `__*` 系统表结构、编号与迁移影响，需要成员二从
**类型与记录协议、物理格式、迁移**角度补齐并确认后，成员二才能落地持久化验证测试。

> 状态：**提案，待三方评审与确认**。未确认前不修改公共代码，也不转换业务目录。
> 本文只约定"元数据怎么存、怎么恢复、怎么迁移"，SQL 语义、Catalog 逻辑与鉴权由成员一/三负责。

## 一、目的与边界

- 成员二负责：元数据的**记录编码、系统表物理结构、重开恢复、格式版本与迁移影响**，以及随后的
  **真实文件持久化/恢复验证**。
- 成员二不负责：视图展开、触发器调度、鉴权、Catalog 业务逻辑（成员三），也不解析 SQL（成员一）。
- 验收规则（分工文档第三/六节）：**不能以模拟存储代替持久化验收**，验证必须走真实文件 + 重开。

## 二、系统表编号与目录入口（关键前置决策）

存储层约束（见 [存储格式设计](storage_format.md) 第 3、5 节）：

- 页 0 的 `table_id → root_page` 映射区**下标即 table_id**，容量约 `(4096-40)/4 = 1014`；
  `table_id` 为无符号 32 位；`__catalog` 固定占 0。
- 数据页头与索引页头都持久化 `table_id`，页链归属依赖它。

成员三提案为系统表**固定保留编号 1..6、用户表从 7 开始**（方案 A）。这对已有库是破坏性改动：

| 方案 | 编号 | 既有库影响 | 优点 | 缺点 |
|---|---|---|---|---|
| A 固定保留段 | `__views`=1…`__dependencies`=6 | 用户表 1..N 与新段冲突，**必须重编号**：`__catalog` 行、页 0 映射区、每个数据页头 `table_id`、索引页头 `table_id`、`next_table_id` 全部改写 | 恢复入口固定，按 id 直接定位 | 一次性破坏性重写业务数据，风险与工作量最大 |
| **B 目录登记（成员二建议）** | 由 `next_table_id` 动态分配，登记进 `__catalog` | 用户表编号不变，仅**新增缺失的系统表** | 不动既有数据页/索引页/映射区，天然向后兼容；恢复入口仍固定（`__catalog` 恒为 0） | 系统表以 `__` 前缀 + `__catalog` 行识别，目录层需一致排除 |

**成员二建议方案 B**，理由：

1. 避免对既有用户数据做破坏性 `table_id` 重映射——该重写会触碰页头与所有持久化引用，是
   本阶段风险最高、收益最低的一步。
2. 恢复路径仍固定且简单：`bootstrap` 先建/恢复 `__catalog`，再扫描 `__catalog` 中名称以
   `__` 开头的行得到已存在系统表的编号，**缺哪个建哪个**（幂等），旧库打开即自动补齐。
3. `next_table_id` 已在页 0 持久化（`DiskPageManager`/`HeapStorage` 维护），动态分配无需新格式。

无论 A/B：

- `list_tables()` 与所有"用户表"入口**必须排除系统表**（`__` 前缀）；
- 系统表禁止被 SQL 直接查询、修改、DROP（错误码沿用 `PROTECTED_TABLE`）；
- 若最终选择 A，成员二需要在迁移工具中实现用户表重编号，并单独评审其崩溃安全与回滚。

## 三、系统表结构

沿用成员三提案的列，按成员二存储视角修订（`__indexes` 补 `root_page`，新增 `__constraints`）：

| 系统表 | 列（固定顺序） | 与成员三提案的差异 |
|---|---|---|
| `__views` | view_id INT, view_name VARCHAR, definition VARCHAR, column_index INT, column_name VARCHAR, column_type VARCHAR | 一致 |
| `__triggers` | trigger_id INT, trigger_name VARCHAR, table_name VARCHAR, event VARCHAR, action VARCHAR, **created_order INT** | 按成员三定稿收敛：首版固定 AFTER，无需 timing；created_order 保证同事件多触发器顺序稳定 |
| `__indexes` | index_id INT, index_name VARCHAR, table_name VARCHAR, unique_flag INT, column_index INT, column_name VARCHAR, **root_page INT** | **新增 root_page**（必需，见 [F09 约定](F09索引接口约定-成员二.md) 第四节） |
| `__users` | user_id INT, account_id VARCHAR, user_name VARCHAR, salt VARCHAR, key VARCHAR, iterations INT, is_admin INT | 一致（salt/key 编码见第四节） |
| `__grants` | user_name VARCHAR, object_type VARCHAR, object_name VARCHAR, permission VARCHAR | 一致 |
| `__dependencies` | object_type VARCHAR, object_name VARCHAR, depends_on_type VARCHAR, depends_on_name VARCHAR | 一致 |
| `__constraints` | table_name VARCHAR, column_index INT, constraint_name VARCHAR, kind VARCHAR, expression VARCHAR, reference_table VARCHAR, reference_columns VARCHAR, default_text VARCHAR | **新增**，补 F07 缺口 |

`__constraints` 补充说明：现有 `__catalog` 只持久化
`(table_id, table_name, column_index, column_name, column_type)` 五列，
**不保存 nullable / default / 约束**。为避免改动 `__catalog` 布局（旧库按固定 5 列读取，
见 `engine/catalog.py` 的 `bootstrap`/`_restore_column`），成员二建议**另立 `__constraints` 表**：

- `kind ∈ {PRIMARY KEY, FOREIGN KEY, UNIQUE, NOT NULL, CHECK, DEFAULT}`，与成员一
  `contracts/extensions.py` 的 `Constraint.kind` 对齐；
- 列级 `NOT NULL`/`DEFAULT` 也登记于此（`column_index` 指向列）；
- 备选：把 nullable/default 编进 `__catalog` 的类型文本（类比现有 `DECIMAL(p,s)` 写法）。
  缺点是类型文本承载语义、旧库解析需版本分支，**成员二不推荐**，留待三方确认。

所有列**仅使用 INT/VARCHAR（含 NULL）**，不依赖 BOOL/DECIMAL/DATE 等新类型，因此：

- V1 编解码器即可读写，旧程序对系统表格式的兼容限制最小；
- **不触发格式版本升级**（仍为 V2），无需迁移业务数据（方案 B 下）。

## 四、记录层编码约定

1. **payload 一律 VARCHAR 文本**，不使用 Python `repr`/`pickle`：
   - `__views.definition`、`__triggers.action`、`__constraints.expression` 存**规范化 SQL 文本**，
     重开后再交给成员一编译，保证与编译期行为一致；
   - 多列名（`reference_columns`、联合索引列）存规范化列名，逗号分隔，**顺序即键序**；
   - 索引列/视图输出列的 `column_index` 用于保持列顺序。
2. **二进制 → 十六进制字符串**：`__users.salt` / `__users.key` 为 `bytes`，编码为小写 hex
   `VARCHAR`；`iterations` 存**账户实际迭代次数**（不写死默认值），`account_id` 存 UUID 文本。
3. **敏感字段不外泄**：`salt`/`key` 不得进入 `--trace`、操作日志、普通查询结果与 EXPLAIN
   输出（由成员三目录层与展示层共同保证；成员二保证存储接口本身不打印行内容）。
4. **空值用 NULL**，"缺失"不以 VARCHAR 空串表达，避免与合法空串定义混淆。
5. **容量**：各系统表行数与其 payload 都很小；单行仍受 4067 字节上限约束，超限报
   `INVALID_RECORD`（长视图/触发器定义按此上限校验）。

## 五、存储层保证（成员二承诺）

| 保证 | 说明 |
|---|---|
| 同接口持久化 | 系统表就是普通表，走 `create_table/insert/delete/scan/flush`，**不新增存储接口、不改格式** |
| 事务一致 | 元数据写入与对应 DDL 处于同一事务，随整文件前映像日志提交/回滚/崩溃恢复 |
| 重开恢复 | `bootstrap` 从系统表重建内存目录（与 `__catalog` 同模式），幂等补齐缺失系统表 |
| 备份/迁移 | 迁移工具的"临时文件重建 + `durable_replace` 原子替换"天然覆盖系统表；方案 B 下用户表编号不变 |
| 格式稳定 | 不升级版本号；系统表只用 INT/VARCHAR，V1/V2 均可读 |
| 隔离 | 系统表不出现在 `list_tables()`，不可被 SQL 直接访问或 DROP |

## 六、成员三接入清单（成员二据此验证）

成员三在 `PersistentCatalog`（`engine/catalog.py`）补齐以下 API，成员二即可写验证测试：

```python
# 视图 F10
register_view(defn: ViewDefinition); unregister_view(name); get_view(name); list_views()
# 触发器 F11
register_trigger(defn: TriggerDefinition); unregister_trigger(name); get_trigger(name); list_triggers()
# 索引 F09（登记时保存 index.root_page，重开据此构造 BTreeIndex）
register_index(defn: IndexDefinition); unregister_index(name); get_index(name); list_indexes(table)
# 账户与授权 F12
register_account(account); update_account(account); remove_account(name); get_account(name); list_accounts()
grant(user, object_type, object_name, permission); revoke(...); list_grants(user)
# 依赖 F10/F11
add_dependency(kind, name, target_kind, target_name); get_dependencies(kind, name)
# 约束 F07
register_constraint(table, constraint); unregister_constraint(table, name); get_constraints(table)
# 目录
list_tables()          # 必须排除全部 `__` 系统表
bootstrap()            # 幂等创建/恢复全部系统表
```

签名应对齐 `contracts/extensions.py` 的 `ExtendedCatalogReader` 协议；成员三新增写入方法时
同步更新该协议与测试替身（`tests/fakes/`），避免三人同改同一公共文件。

## 七、成员二验证计划（Catalog 落地后执行）

新增真实文件测试（`tests/storage/test_metadata_persistence.py` 或 `tests/integration/`），**全部使用
真实 `minisql.db`**：

1. **写入→重开一致**：各类对象经 Catalog 登记 → `flush` → `close` → 重开 → 逐字段读回一致；
2. **事务回滚**：显式事务内登记后 `ROLLBACK`，对象不存在且文件恢复；
3. **崩溃恢复**：子进程登记后 `os._exit()`，重开后对象一致（沿用 `tests/integration/test_recovery.py` 模式）；
4. **迁移/备份**：含系统表的库迁移后对象定义保留、`table_id` 引用有效；
5. **敏感字段**：`salt`/`key` 不出现在 `--trace`、日志或查询结果；
6. **边界**：超长视图/触发器定义按 4067 字节上限报错，不写坏数据。

**执行状态（2026-09-11）**：

- **已执行**：① 写入→重开一致（`tests/integration/test_object_persistence.py` 覆盖视图/触发器/索引含
  `root_page`/依赖/账户/授权/约束，`tests/storage/test_metadata_persistence.py` 覆盖系统表记录）；
  ④ 迁移/备份保留（`tests/storage/test_migrate.py::test_apply_preserves_system_tables`）；
  ⑥ 边界（超单页容量按 `INVALID_RECORD` 拒绝）。
- **未执行**：② 事务回滚、③ 崩溃恢复、⑤ 敏感字段日志排除。三项均依赖成员三把元数据写入接入
  事务日志与上层过滤；当前 `engine/objects.py` 写入直接 `flush()`、未走事务，尚无法验证回滚与
  崩溃恢复，故不计入成员二已完成验收。

## 八、待三方确认

1. **编号方案 A/B**（第二节）：成员二建议 B；若选 A，需确认迁移重编号的验收与回滚要求。
2. **`__constraints` 独立表 vs 扩展 `__catalog`**（第三节）：成员二建议独立表。
3. **`__indexes.root_page` 列**：呼应 [F09 约定](F09索引接口约定-成员二.md)第五节，请成员三确认。
4. **`ViewDefinition.query` 的持久化形态**（SQL 文本 vs 结构化）：成员一定稿，成员二按其编码。
5. **系统表可见性与排除规则**（查询、EXPLAIN、GUI、`list_tables`）：成员三定稿。
6. **敏感列访问与日志排除**的责任边界：成员二保证存储不打印，成员三保证上层过滤。

## 九、状态

- [x] 成员二：新类型/NULL 编解码、V1/V2 格式、`rewrite_table`、B+ 树、真实夹具、迁移工具（前提项已就绪）。
- [x] 成员二：本持久化契约草案（编号、结构、编码、恢复、迁移、验证计划）。
- [x] 成员二：系统表物理结构与记录编解码落地（`storage/metadata.py`，见第十节）。
- [x] 成员二：迁移保留系统表（`engine/migrate.py` 改为枚举用户表 + 系统表，见第十节）。
- [x] 成员二/三：系统表结构收敛为单一来源，`__indexes.root_page` 与 `__constraints` 落地（见第十一节）。
- [x] 成员三：`engine/objects.py` 实现系统表读写（`PersistentObjectCatalog`/`PersistentAccountStore`），
      `bootstrap` 幂等补齐；`catalog.list_tables()` 已排除 `__` 前缀。
- [x] 成员二：真实文件持久化/恢复验证（视图/触发器/索引/依赖/账户/授权/约束 + 迁移保留，见 §7 说明）。
- [ ] 三方确认本草案（尤其编号方案与 `__constraints`/`root_page`）。
- [ ] 成员三：§7 的事务回滚/崩溃恢复/敏感字段日志排除（需将元数据写入接入事务与上层过滤）。

## 十、成员二落地进展与对其他成员的提醒（2026-09-11）

### 已交付（成员二自有范围，未改动成员三的公共契约）

| 交付 | 位置 | 说明 |
|---|---|---|
| 系统表物理结构 | `src/minisql/storage/metadata.py` | `__views/__triggers/__indexes/__users/__grants/__dependencies/__constraints` 七张表定义（`SYSTEM_TABLES`），`__indexes` 已含 `root_page`，全部列只用 INT/VARCHAR |
| 记录层编解码 | 同上 | `encode_bytes/decode_bytes`（salt/key 十六进制）、`encode_name_list/decode_name_list`（顺序即键序、空列表记 NULL）、`is_system_table`（`__` 前缀） |
| 迁移保留元数据 | `src/minisql/engine/migrate.py` | `_survey`/`_rebuild` 改用 `_object_schemas()` 枚举"用户表 + 系统表"；原先用 `list_tables()` 会漏掉系统表，导致视图/索引/账户元数据迁移后丢失 |
| 真实文件验证 | `tests/storage/test_metadata_persistence.py` | 七张表写入→flush→关闭→重开逐字段读回；账户字节十六进制还原；联合列名顺序；NULL 与空串区分；长定义往返；超单页容量按 `INVALID_RECORD` 拒绝 |
| 迁移验证 | `tests/storage/test_migrate.py::test_apply_preserves_system_tables` | 含 `__indexes` 的 V1 库迁移到 V2 后，系统表结构、编号、`root_page` 行均保留 |

本地全套 **935 项通过**（含存储专项）。上述模块只提供"怎么存"的物理能力，不含 Catalog 业务逻辑。

### 提醒成员三（阻塞项，实现 Catalog 时必须照做）

1. **`list_tables()` 必须按 `__` 前缀排除系统表**。当前 `engine/catalog.py` 用 `schema.table_id != 0`
   过滤；方案 B 下系统表编号 ≥1，会被当作用户表返回。请改用 `storage.metadata.is_system_table(name)`
   判断，并同步检查 GUI/EXPLAIN 等所有"用户表"入口。
2. **`bootstrap` 幂等补齐**：遍历 `storage.metadata.SYSTEM_TABLES`，扫描 `__catalog` 中已存在的
   `__` 前缀行得到编号，缺哪个 `create_table` + `register_table` 哪个；已存在则跳过。
3. **`__indexes.root_page`**：登记索引时必须在同一事务写入 `index.root_page`，否则重开无法构造
   `BTreeIndex(pages, buffer, columns, root_page, table_id)`（详见 [F09 约定](F09索引接口约定-成员二.md)）。
4. **`__constraints` 承载 F07**：primary/foreign/unique/not null/check/default 全部登记于此，
   不动 `__catalog` 的固定 5 列布局。
5. **系统表保护**：禁止 SQL 直接 `SELECT/INSERT/UPDATE/DELETE/DROP`，错误码沿用 `PROTECTED_TABLE`。

### 提醒成员一

- 请定稿 `ViewDefinition.query` 的持久化形态（SQL 文本 vs 结构化）。成员二按"`__views.definition`
  存规范化 SQL 文本、重开后交编译器重新编译"的约定编码；若改为结构化，需同步本约定与编解码。
- `Constraint.kind` 取值集合请与 `__constraints.kind`（`PRIMARY KEY/FOREIGN KEY/UNIQUE/NOT NULL/CHECK/DEFAULT`）
  保持一致。

### 尚需三方确认（不阻塞成员二当前交付）

- 编号方案 A/B（成员二已按 B 实现迁移；若改 A 需另做用户表重编号与回滚评审）；
- `__constraints` 独立表 vs 扩展 `__catalog`；`__indexes.root_page`；系统表可见性与敏感列日志排除边界。

## 十一、未收敛项的处理结果（2026-09-11 收敛）

成员三落地 `engine/objects.py` 后，双方系统表定义出现分歧，已按下表收敛。**系统表物理结构的唯一来源为
`src/minisql/storage/metadata.py`**，`engine/objects.py` 只按名字引用（`VIEWS_CATALOG = metadata.VIEWS` 等），
不再复制列定义，杜绝再次分歧。

| 未收敛项 | 处理 | 位置 |
|---|---|---|
| `__triggers` 列分歧（`timing/created_at` vs `created_order`） | 采用成员三定稿：`event, action, created_order`；`metadata.TRIGGERS` 已同步 | `storage/metadata.py` |
| `__indexes` 缺 `root_page`（F09 硬前提） | 已补 `root_page INT`；`IndexDefinition.root_page` 落盘并在重开时恢复 | `metadata.INDEXES`、`objects.py` 的 `register_index/_restore` |
| 缺 `__constraints`（F07 落点） | 已建表并实现 `register/get/unregister_constraint`（按列名解析列序号，一列一行） | `metadata.CONSTRAINTS`、`objects.py` |
| 系统表重复定义 | `objects.py` 改为引用 `metadata` 常量，单一来源 | `engine/objects.py` |

**仍属成员三执行器责任（本次未做，不属"未收敛的接口"）**：CREATE INDEX 构建后用 `index.root_page`
回填登记；写入路径的实际约束校验、默认值展开与存量数据校验；DROP TABLE 时清理 `__constraints`/`__indexes`。
`__constraints.kind` 取值须与成员一 `Constraint.kind` 一致（PRIMARY KEY/FOREIGN KEY/UNIQUE/NOT NULL/CHECK/DEFAULT）。

验收：新增 `tests/integration/test_object_persistence.py` 的 root_page 与约束重开用例；
`tests/storage/test_metadata_persistence.py` 同步触发器列；全套 **950 项通过**。
