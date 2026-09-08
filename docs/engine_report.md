# 执行引擎与 CLI 模块报告（成员三）

## 1. 目的

实现 MiniSQL 的执行引擎与用户入口：以算子执行器、持久化 Catalog 和数据库生命周期
串联编译器与页式存储，并提供交互式/文件式 CLI，贯通 SQL → Plan → 执行 → 磁盘的
后半段链路，支撑建表、插入、查询、删除与关闭重开的端到端验收。

## 2. 模块职责与交付物

| 组件 | 文件 | 职责 |
|---|---|---|
| PlanExecutor | engine/executor.py | 六种算子执行、表达式求值、严格类型检查 |
| PersistentCatalog | engine/catalog.py | 系统表 bootstrap/恢复、表注册与查询 |
| Database | engine/database.py | 逐语句编译/执行/刷新、open_database 装配、关闭 |
| CLI | cli/main.py | 交互模式、SQL 文件执行、结果表格与错误输出 |

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

### 3.3 建表顺序与刷新策略

- 先 storage.create_table 分配稳定 table_id，成功后再 register_table 登记目录，
  登记失败不写入元数据。
- 每条成功语句执行后 storage.flush；close 时 flush 后 close，刷新错误不吞掉。
- execute 遇错抛出 MiniSQLError 并停止，之前成功操作保持有效，不返回部分结果。

### 3.4 表达式求值与类型检查

- 比较要求同类型操作数；排序比较支持同类型（与语义分析、常量折叠规则一致）。
- AND/OR/NOT 要求 BOOL，加减要求 INT；bool 不因 Python 继承关系被当作 INT。
- WHERE 结果必须为 BOOL，否则报 TYPE_MISMATCH。

### 3.5 CLI 行为

- 文件模式一次执行整个文件，逐语句展示结果；交互模式以行尾分号判断语句结束。
- MiniSQLError 输出到 stderr 并退出码 1；依赖模块未实现时如实报告并退出码 2。
- 正常与异常退出均调用 close，stdin 不可读按 EOF 正常退出。

## 4. 测试记录

### 4.1 测试文件与数量

| 文件 | 场景 | 数量 |
|---|---|---|
| tests/engine/test_acceptance.py | 算子全流程、目录、RecordId、类型错误（含编译器方补充的排序边界） | 25 |
| tests/integration/test_acceptance.py | 多语句执行、遇错停止、重启恢复、core.sql、错误定位 | 10 |
| tests/integration/test_cli.py | 结果渲染、文件模式、交互模式、错误输出 | 7 |
| tests/integration/test_errors.py | errors.sql 六类错误场景（独立环境） | 6 |

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
| CLI 文件模式、交互模式、错误输出、EOF 退出 | 通过 |

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

关闭后新进程重开，数据与目录完整恢复。全量测试 227 项全部通过、零跳过。

## 5. 关键技术难点与解决

1. **RecordId 丢失风险**：Project 产生结果列会丢弃 RecordId，删除路径若经过投影
   将无法定位记录。解决：Delete.source 计划类型限定 + 执行器运行时双重检查。
2. **空表时的投影列校验**：最初在行循环内检查未知列，空表不触发。解决：先对
   全部投影列做存在性校验，再逐行投影。
3. **目录恢复绕过编译**：重启时编译器尚未就绪也可能需要恢复目录。解决：
   bootstrap 按固定系统表结构直接读行重建，不经过 SQL 编译。
4. **对未实现依赖的如实报告**：联调完成前 CLI 需区分「业务错误」与「依赖未实现」，
   后者如实报告退出码 2；依赖落地后该路径自然退场，避免伪造成功。

## 6. 结论与小结

执行引擎与 CLI 已独立完成并通过全部单元、集成与错误验收；与编译器、页式存储
联调后 227 项测试全绿，core.sql 端到端演示、六类错误诊断与关闭重启持久化均验收
通过，满足建表→插入→查询→删除→再查→关闭→重开查询的综合验收要求。
