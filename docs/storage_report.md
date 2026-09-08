# 存储系统模块报告（成员二）

> 本文件为报告章节框架，标注「待补全」的部分需在联调完成后填写。

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

## 3. 关键设计决策

### 3.1 页格式（slotted page）

- 页大小 4096 字节，页头 24 字节、槽目录每项 5 字节。
- 槽目录向下增长、记录数据向上增长，中间为空闲空间。
- 页头字段：page_id / page_type / slot_count / free_start / data_end /
  next_free_page / next_data_page。
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
| tests/storage/test_acceptance.py | 9 个验收场景 | 9 |

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

### 4.3 持久化证据

- 关闭文件后重新打开，页分配器、空闲链表、表根页映射与记录均完整恢复。
- （待补全：真实文件重开测试截图 / 命令输出）

## 5. 关键技术难点与解决

1. **根页映射对齐问题**：最初设计 `root_page = table_id + 1` 纯函数映射，
   发现数据页扩展会消耗页号导致映射失效，改为页 0 显式映射区。
2. **脏页淘汰顺序**：脏页必须先写回再淘汰，否则丢失未落盘修改。
3. **中文字符串边界**：VARCHAR 长度前缀按 UTF-8 字节数计，编解码两侧一致。

## 6. 结论与小结

存储层已独立完成并通过全部单元测试与验收场景，可支撑上层执行引擎的
RecordStorage 协议调用。（待补全：与编译器、引擎联调后的最终结论）

## 7. AI 辅助使用说明

- 使用 AI 辅助进行设计分析、代码生成与单元测试编写。
- 关键设计决策（页格式、根页映射、缓冲池策略）经人工确认后落地。
- （待补全：具体 AI 工具与使用方式说明）
