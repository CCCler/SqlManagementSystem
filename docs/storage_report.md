# 存储系统模块报告（成员二）

> 存储系统模块交付报告，数据均来自本地实测（Python 3.12 / pytest 8.3）。

## 1. 目的

实现 MiniSQL 的页式存储层：以 4KB 页为单位持久化表数据与 Catalog，提供
文件读写、页管理、记录编解码、缓冲池与堆存储能力，支撑编译器与执行引擎的上层功能。

## 2. 模块职责与交付物

| 组件 | 文件 | 职责 |
|---|---|---|
| FileManager | storage/file_manager.py | 文件随机读写、同步、关闭 |
| DiskPageManager | storage/page.py | 页分配/释放/读写、页 0 元信息、空闲页链表 |
| RowCodec | storage/record.py | INT/VARCHAR 记录编解码 |
| PageBufferPool | storage/buffer.py | LRU/FIFO 缓存、脏页写回、统计与替换日志 |
| HeapStorage | storage/record.py | 表根页映射、记录插入/扫描/删除、持久化 |
| RollbackJournal | storage/journal.py | 数据库级锁、写前回滚日志、提交同步与崩溃恢复 |

## 3. 关键设计决策

### 3.1 页格式（slotted page）

- 页大小 4096 字节，页头 24 字节、槽目录每项 5 字节。
- 槽目录向下增长、记录数据向上增长，中间为空闲空间。
- 页头字段：page_id / page_type / slot_count / free_start / data_end /
  next_free_page / next_data_page / table_id。
- 页类型：FREE（空闲链表）、DATA（数据页，next_data_page 串联同表数据页）、
  META（页 0 元信息页）。

### 3.2 空闲页管理

- 采用空闲页链表（页头 next_free_page 串联），页 0 元信息区持久化链表头。
- 分配时优先复用链表头页，链表空时递增 next_page_id。

### 3.3 table_id 与根页映射

- table_id=0 保留给 `__catalog`，用户表从 1 开始由 next_table_id 递增。
- 根页由 allocate_page 独立分配，不依赖页号与 table_id 对齐
  （数据页扩展会消耗页号，纯函数映射会失效）。
- `table_id -> root_page` 显式持久化在页 0 的映射区（下标 = table_id）。

### 3.4 记录编码

- INT：8 字节有符号大端（>q），范围 [-2^63, 2^63-1]。
- VARCHAR：2 字节长度前缀（按字节）+ UTF-8。
- bool 不得当作 INT 存储；记录不跨页，单条最大 4067 字节。

### 3.5 缓冲池

- 用 OrderedDict 统一实现 LRU 与 FIFO：LRU 命中 move_to_end，FIFO 命中不动。
- 脏页淘汰前先写回；记录 CacheStats 与 ReplacementEvent 日志。

## 4. 测试记录

### 4.1 单元测试

| 文件 | 场景 | 数量 |
|---|---|---|
| tests/storage/test_phase1.py | FileManager、RowCodec、DiskPageManager | 17 |
| tests/storage/test_phase2.py | PageBufferPool、HeapStorage | 13 |
| tests/storage/test_acceptance.py | 验收场景 | 9 |
| tests/storage/test_reclaim.py | 删除空间回收、槽复用、页链释放 | 18 |
| tests/storage/test_cache_experiment.py | LRU/FIFO 缓存对比实验 | 10 |

### 4.2 验收场景

| 场景 | 结果 |
|---|---|
| allocate_free_reuse_page（页分配/释放/复用） | 通过 |
| cross_page_scan（跨页扫描） | 通过 |
| unicode_row_roundtrip（中文字符串往返） | 通过 |
| oversized_row_rejected（过大记录拒绝） | 通过 |
| lru_replacement（LRU 替换） | 通过 |
| fifo_replacement（FIFO 替换） | 通过 |
| dirty_eviction_flush（脏页淘汰刷盘） | 通过 |
| hit_statistics（命中统计） | 通过 |
| reopen_records（关闭重开恢复） | 通过 |

### 4.3 缓存统计证据

用 `python -m minisql.cli.cache_experiment`（独立临时页文件，访问序列
`1,2,1,3,1,2,4,1,2,3`，写入页 `{1,2}`，3 轮）对比 LRU 与 FIFO：

| 策略 | 容量 | 生命周期 | 命中率 | 命中 | 未命中 | 淘汰 | 写回 |
|---|---|---|---|---|---|---|---|
| LRU | 2 | continuous | 0.200 | 6 | 24 | 22 | 15 |
| FIFO | 2 | continuous | 0.100 | 3 | 27 | 25 | 18 |
| LRU | 3 | continuous | 0.700 | 21 | 9 | 6 | 6 |
| FIFO | 3 | continuous | 0.500 | 15 | 15 | 12 | 12 |
| LRU | 4 | continuous | 0.867 | 26 | 4 | 0 | 6 |
| FIFO | 4 | continuous | 0.867 | 26 | 4 | 0 | 6 |
| LRU | 2 | reset-per-round | 0.200 | 6 | 24 | 18 | 15 |
| FIFO | 2 | reset-per-round | 0.100 | 3 | 27 | 21 | 18 |
| LRU | 3 | reset-per-round | 0.500 | 15 | 15 | 6 | 6 |
| FIFO | 3 | reset-per-round | 0.300 | 9 | 21 | 12 | 12 |
| LRU | 4 | reset-per-round | 0.600 | 18 | 12 | 0 | 6 |
| FIFO | 4 | reset-per-round | 0.600 | 18 | 12 | 0 | 6 |

结论：容量不足时 LRU 明显优于 FIFO（容量 3 连续访问命中率 0.700 对 0.500）；
容量足够覆盖工作集时二者一致；持续复用缓存（continuous）优于每轮重建（reset-per-round）。

### 4.4 更大规模性能基准

用 `python examples/bench_storage.py`（独立临时文件，单表两列 INT+VARCHAR，
缓存容量 64）批量插入 / 扫描 / 删除。修复插入与删除路径后实测：

| 规模 | 插入耗时 | 扫描耗时 | 删除耗时 | 数据页数 | 缓存未命中 |
|---|---|---|---|---|---|
| 5,000 条 | 332 ms | 14.6 ms | 472 ms | 31 | 30 |
| 10,000 条 | 682 ms | 29.7 ms | 905 ms | 60 | 59 |
| 20,000 条 | 1.18 s | 59.4 ms | 1.80 s | 122 | 362 |

20,000 条时插入吞吐约 16,884 行/秒、删除约 5,563 行/秒，文件 499,712 字节
（122 页），缓存命中率 0.9955。

**扩展性问题修复**：优化前插入与删除耗时均随规模平方增长（20,000 条插入
101.9 s、删除 6.63 s，未命中 127 万）。根因与修复：

1. `insert` 每次从根页沿 `next_data_page` 线性扫描到首个有空闲的页，复杂度 O(N²)。
   修复为维护「插入候选页指针」（复用根页头 `next_free_page` 持久化）：插入从
   候选页开始、填满后前进，删除使靠前页出现空洞时回拨。
2. `delete` 为校验 `RecordId` 归属，每次从根页遍历到目标页，复杂度 O(N·页数)。
   修复为在页头 `reserved` 区持久化 `table_id`，删除直接按 `page_id` 定位并 O(1)
   校验（页号范围、页类型、所属表、槽有效）。

修复后插入与删除均近似线性，缓存未命中从 127 万降至 362。

### 4.5 持久化证据

- 关闭文件后重新打开，页分配器、空闲链表、表根页映射与记录均完整恢复。
- 真实文件重开演示（`python examples/demo.py`）命令输出：

  ```
  == 1. 执行 core.sql（建表 -> 插入 -> 查询 -> 删除 -> 再查） ==
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

  == 2. 关闭后重新打开查询（持久化验证，预期只剩 Bob） ==
  id | name
  ---+-----
  2  | Bob
  ```

  关闭后重开只查到 Bob，证明插入与删除均已正确落盘。

## 5. 关键技术难点与解决

1. **根页映射对齐问题**：最初设计 `root_page = table_id + 1` 纯函数映射，
   发现数据页扩展会消耗页号导致映射失效，改为页 0 显式映射区。
2. **脏页淘汰顺序**：脏页必须先写回再淘汰，否则丢失未落盘修改。
3. **中文字符串边界**：VARCHAR 长度前缀按 UTF-8 字节数计，编解码两侧一致。
4. **插入的 O(N²) 页链扫描**：`insert` 每次从根页线性扫描到尾页，数据量增大时
   耗时平方增长。通过持久化「插入候选页指针」（复用根页头 `next_free_page`），
   插入从候选页开始、删除时回拨靠前空洞，已消除平方增长，详见 4.4。
5. **删除的 O(N·页数) 归属校验**：`delete` 为校验 `RecordId` 归属每次从根页遍历
   到目标页。通过在页头 `reserved` 区持久化 `table_id`，删除直接定位并 O(1)
   校验，消除平方增长，详见 4.4。

## 6. 结论与小结

存储层已独立完成并通过全部单元测试与验收场景，可支撑上层执行引擎的
RecordStorage 协议调用。与编译器、执行引擎联调后，建表、插入、扫描、删除、
关闭重开、事务与崩溃恢复均在真实文件中通过，全套 355 项测试通过、无跳过。
大规模基准曾暴露插入与删除的 O(N²) 问题，已分别通过插入候选页指针与页头
归属校验修复，插入与删除均近似线性。

## 7. AI 辅助使用说明

- 使用 AI 辅助进行设计分析、代码生成与单元测试编写。
- 关键设计决策（页格式、根页映射、缓冲池策略）经人工确认后落地。
- 具体工具与使用方式（请按实际补充）：以 AI 助手完成页格式与缓冲池方案讨论、
  RowCodec / HeapStorage / 回滚日志及测试用例的代码生成；事务恢复边界与性能
  基准脚本由 AI 协助编写，所有正确性结论均经本地运行测试验证。
