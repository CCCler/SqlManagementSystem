# 执行引擎与 CLI 模块报告（成员三）

## 1. 目的

实现 MiniSQL 的执行引擎与用户入口：以算子执行器、持久化 Catalog、事务装配和数据库
生命周期串联编译器与页式存储，并提供交互式/文件式 CLI，贯通 SQL → Plan → 执行 → 磁盘
的后半段链路，支撑建表、插入、查询、删除、删表、事务、关闭重开与异常恢复的端到端验收。

## 2. 模块职责与交付物

| 组件 | 文件 | 职责 |
|---|---|---|
| PlanExecutor | engine/executor.py | 七种算子执行（含 DropTable）、表达式求值、严格类型检查 |
| PersistentCatalog | engine/catalog.py | 系统表 bootstrap/恢复、表注册、注销与查询 |
| Database / TransactionalDatabase | engine/database.py | 逐语句编译/执行/刷新、open_database 装配、自动提交与显式事务、进程锁与日志装配、错误传播 |
| CLI | cli/main.py | 交互模式、SQL 文件执行、结果表格、错误输出、--trace/--lock-timeout |

## 3. 关键设计决策

### 3.1 RecordId 保留策略

- SeqScan/Filter 传递 StoredRecord，全程保留 RecordId；Project 只产生结果列。
- Delete 的 source 在计划类型上限定为 SeqScan/Filter，执行器对 Project 源
  追加运行时检查（报 UNKNOWN_PLAN），双保险保证删除不经过投影。

### 3.2 Catalog 持久化与恢复

- 系统表固定五列结构（table_id/table_name/column_index/column_name/column_type），
  table_id=0 保留；用户表从 1 开始。
- bootstrap 幂等：create_table 报 DUPLICATE_TABLE 视为系统表已存在，随后
  直接扫描系统表、按 column_index 排序重建 TableSchema，不经过 SQL 编译。
- 表名统一小写键，与 MemoryCatalog 测试替身行为一致，保证替身与真实目录可互换。
- register_table 逐列写入元数据行后刷新；unregister_table 支持删表时目录持久化注销。

### 3.3 建表/删表顺序与刷新策略

- 建表先 storage.create_table 分配稳定 table_id，成功后再 register_table 登记目录，
  登记失败不写入元数据；删表先校验再物理释放、最后注销目录。
- 每条成功语句执行后 storage.flush；正常 close 刷新后关闭，刷新错误不吞掉。
- execute 遇错抛出 MiniSQLError 并停止，之前成功操作保持有效，不返回部分结果。

### 3.4 DropTable 保护

- 拒绝删除系统目录表（table_id=0 / `__catalog`），报 PROTECTED_TABLE。
- 执行前用目录当前状态复核 table_id：目录不存在或 table_id 与计划不一致时
  报 UNKNOWN_TABLE，防止「删表失败后同名重建」的旧计划误删新表。
- 物理释放（成员二的整表页链回收）与目录注销顺序固定：先释放存储、再注销元数据。

### 3.5 事务装配与错误传播（成员三 Database 层）

- open_database 装配 FileManager、DiskPageManager、PageBufferPool、HeapStorage、
  PersistentCatalog、SQLCompiler、PlanExecutor，并装配进程锁与回滚日志后 bootstrap。
- 默认自动提交：每条语句在自身事务中执行并提交；BEGIN/COMMIT/ROLLBACK 进入显式
  事务；显式事务期间持有数据库独占锁，其他连接等待后报 DATABASE_BUSY。
- 自动提交中任何失败（含 I/O 故障）整体回滚并释放锁，连接保持可用；显式事务
  中出错标记 TRANSACTION_ABORTED，必须先 ROLLBACK；跨线程操作显式事务报
  TRANSACTION_OWNER。
- 业务错误原样传播；OSError 统一转换为 MiniSQLError(storage, IO_ERROR)，保留
  业务错误与 I/O 错误的区分；提交标记写入未确认时报 COMMIT_UNCERTAIN 并使连接
  失效（后续执行报 CONNECTION_CLOSED），要求关闭重开核实。
- 正常与异常退出均调用 close；显式事务未提交时退出自动回滚并提示；刷新/关闭
  错误不吞掉。

### 3.6 表达式求值与类型检查

- 比较要求同类型操作数；排序比较支持同类型（与语义分析、常量折叠规则一致）。
- AND/OR/NOT 要求 BOOL，加减要求 INT；bool 不因 Python 继承关系被当作 INT。
- WHERE 结果必须为 BOOL，否则报 TYPE_MISMATCH。

### 3.7 CLI 行为

- 文件模式一次执行整个文件，逐语句展示结果；交互模式以词法状态识别语句边界
  （字符串/注释内的分号不切分），续行显示 `...>` 提示。
- EOF/退出时有未完成输入则说明原因（缺少结束分号/字符串未闭合/块注释未闭合）
  并返回退出码 1；Ctrl+C 取消未完成输入回到主提示符；同批遇错停止并清空尾部。
- MiniSQLError 输出到 stderr 并退出码 1；依赖模块未实现时如实报告并退出码 2。
- `--trace` 以 JSON Lines 输出编译全阶段与执行结果；`--lock-timeout` 设置锁等待
  秒数（有限非负数），退出时未提交事务自动回滚并提示。

## 4. 测试记录

### 4.1 测试文件与数量（引擎与 CLI 共 143 项）

| 文件 | 场景 | 数量 |
|---|---|---|
| tests/engine/test_acceptance.py | 算子全流程、目录、RecordId、类型错误（含编译器方补充的排序边界） | 25 |
| tests/integration/test_acceptance.py | 多语句执行、遇错停止、重启恢复、core.sql、错误定位 | 10 |
| tests/integration/test_cli.py | 结果渲染、文件模式、交互模式、错误输出 | 7 |
| tests/integration/test_errors.py | errors.sql 六类错误场景（独立环境） | 6 |
| tests/integration/test_cli_input.py | 注释/字符串/块注释、续行提示、未完成输入诊断、Ctrl+C | 21 |
| tests/integration/test_transactions.py | 事务编译、提交/回滚、故障注入、多连接互斥、线程归属 | 24 |
| tests/integration/test_recovery.py | 子进程强退、多进程竞争、恢复再次中断 | 18 |
| tests/integration/test_drop_table.py | 删表、重启、页复用、CLI 与错误场景 | 18 |
| tests/integration/test_trace.py | --trace 输出各阶段 JSON | 6 |
| tests/integration/test_io_boundaries.py | 打开路径不可用、初始化失败锁释放、写故障回滚、提交不确定、句柄释放、flush 错误不吞掉 | 8 |

### 4.2 验收场景

| 场景 | 结果 |
|---|---|
| manual_create_insert_select_delete（人工计划） | 通过 |
| filter_retains_record_id（过滤保留 RecordId 后删除） | 通过 |
| catalog_bootstrap_and_reload（目录初始化与恢复） | 通过 |
| unknown_plan_error（未知计划报错） | 通过 |
| multi_statement_create_then_insert（多语句顺序执行） | 通过 |
| stop_after_error（遇错停止且已成功操作保留） | 通过 |
| restart_data_and_catalog（真实页存储关闭重开） | 通过 |
| core_sql_sequence_and_real_restart（core.sql 全流程 + 重启） | 通过 |
| whole_file_error_position（文件级错误定位 3:8） | 通过 |
| errors.sql 六类错误（未知列/类型不匹配/缺列/语法/非法字符/未闭合字符串） | 通过 |
| 事务：显式提交/回滚含 DDL、出错中止、无事务控制报错、嵌套拒绝、线程归属 | 通过 |
| 并发：多连接串行、DATABASE_BUSY、线程写入无丢失行 | 通过 |
| 恢复：子进程强退、多进程竞争、恢复再次中断 | 通过 |
| DROP TABLE：系统表保护、重启保持、页复用、旧计划防护 | 通过 |
| 异常 I/O：打开失败清理释放锁、写故障自动回滚且连接存活、COMMIT_UNCERTAIN 连接失效、关闭后句柄释放、flush/close 错误不吞掉 | 通过 |
| CLI：文件模式、交互模式、续行提示、未完成输入诊断、错误输出、EOF 退出、--trace、--lock-timeout | 通过 |

### 4.3 端到端演示证据

真实 CLI 运行 `examples/core.sql`（真实编译器 + 真实页存储 + 真实文件）：

```
表 student 已创建
已插入 1 行
已插入 1 行
id | name
---+------
1  | Alice
已删除 1 行
id | name
---+-----
2  | Bob
=== 关闭后重新打开查询 ===
id | name
---+-----
2  | Bob
```

关闭后新进程重开，数据与目录完整恢复；事务回滚、删表与错误诊断的演示步骤见
`examples/demo.py`。全量测试 362 项全部通过、零跳过。

## 5. 关键技术难点与解决

1. **RecordId 丢失风险**：Project 产生结果列会丢弃 RecordId，删除路径若经过投影
   将无法定位记录。解决：Delete.source 计划类型限定 + 执行器运行时双重检查。
2. **空表时的投影列校验**：最初在行循环内检查未知列，空表不触发。解决：先对
   全部投影列做存在性校验，再逐行投影。
3. **目录恢复绕过编译**：重启时编译器尚未就绪也可能需要恢复目录。解决：
   bootstrap 按固定系统表结构直接读行重建，不经过 SQL 编译。
4. **旧计划误删同名重建表**：删表失败或回滚后同名重建，重放旧 DropTable 计划会
   误删新表。解决：执行前用目录当前状态复核 table_id 一致性。
5. **I/O 错误与业务错误混淆**：页写失败若直接抛出 OSError，CLI 与调用方无法区分
   阶段与恢复方式。解决：execute 统一转换为 storage/IO_ERROR；提交标记写入未确认
   单独报 COMMIT_UNCERTAIN 并使连接失效，避免误判回滚成功。
6. **异常路径资源泄漏**：打开失败、执行失败、提交不确定三种路径都必须释放文件
   句柄与进程锁，否则 Windows 上句柄被占导致无法重开。解决：构造函数失败清理、
   失败即回滚并释放锁、close 幂等；专项测试用 unlink 验证句柄真实释放。
7. **对未实现依赖的如实报告**：联调完成前 CLI 需区分「业务错误」与「依赖未实现」，
   后者如实报告退出码 2；依赖落地后该路径自然退场，避免伪造成功。

## 6. 结论与小结

执行引擎与 CLI 已完成：七种算子、持久化目录、事务装配与恢复接线、增强交互；与
编译器、页式存储联调后 362 项测试全绿、零跳过，core.sql 端到端演示、六类错误诊断、
关闭重启持久化、事务回滚、删表页复用及异常 I/O 资源释放均验收通过，满足建表→插入→
查询→删除→再查→关闭→重开查询以及事务与恢复扩展的综合验收要求。
