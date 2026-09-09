# 存储格式设计（成员二）

本文档记录存储层磁盘格式，供成员三联调时查阅。业务代码不应依赖本文档之外的具体字节布局，
只通过 `RecordStorage` / `PageManager` 协议交互。

## 1. 页格式

- 默认页大小 `PAGE_SIZE = 4096` 字节。
- 每页分为三部分：页头、槽目录、记录数据区。
- 槽目录从页头之后向下增长，记录数据从页尾向上增长，两者之间为空闲空间。

```
偏移 0          页头（24 字节）
偏移 24         槽目录（每项 5 字节，向下增长）
                ...
                ↓ 空闲空间 ↑
                ...
偏移 4095       记录数据（向上增长）
```

### 1.1 页头（24 字节）

struct 格式 `>IBHHHiiIB`：

| 字段 | 大小 | 说明 |
|---|---|---|
| page_id | 4B | 页编号，无符号 |
| page_type | 1B | 0=FREE 1=DATA 2=META |
| slot_count | 2B | 当前槽数量 |
| free_start | 2B | 槽目录结束偏移（空闲空间起点） |
| data_end | 2B | 记录数据区结束偏移（空闲空间终点） |
| next_free_page | 4B | 空闲页链表下一节点，-1 表示空/尾；DATA 根页复用为插入候选页 |
| next_data_page | 4B | 同表数据页链表下一节点，-1 表示尾 |
| table_id | 4B | 数据页所属表编号，删除时 O(1) 归属校验；META/FREE 页为 0 |
| reserved | 1B | 保留，置零 |

### 1.2 槽目录（每项 5 字节）

struct 格式 `>HHB`：

| 字段 | 大小 | 说明 |
|---|---|---|
| offset | 2B | 记录在页内的偏移 |
| length | 2B | 记录字节长度 |
| flags | 1B | bit0=已删除（`SLOT_DELETED`） |

## 2. 页类型

| 类型 | 值 | 说明 |
|---|---|---|
| FREE | 0 | 空闲页，页头 `next_free_page` 串联空闲链表 |
| DATA | 1 | 数据页，含槽目录与记录，`next_data_page` 串联同表数据页 |
| META | 2 | 元信息页，仅页 0 |

## 3. 页 0 元信息页

页 0 固定为元信息页，页头之后依次为元信息区与 root_page 映射区。

### 3.1 元信息区（偏移 24，共 16 字节）

struct 格式 `>IIIi`：

| 字段 | 大小 | 说明 |
|---|---|---|
| magic | 4B | 魔数 `0x4D53514C`（"MSQL"），用于识别有效文件 |
| next_page_id | 4B | 下一个可分配的页编号 |
| next_table_id | 4B | 下一个可分配的表编号（HeapStorage 维护） |
| free_list_head | 4B | 空闲页链表头，-1 表示空 |

### 3.2 root_page 映射区（偏移 40 起，每项 4 字节）

- 下标 = table_id，值 = 该表根页号；`0` 表示未分配。
- struct 格式 `>I`。
- `table_id=0` 对应 `__catalog`，用户表从 1 开始，下标连续。
- 最大可映射表数约 `(4096 - 40) / 4 = 1014`。

仅空文件由 `DiskPageManager` 自动初始化为
`magic=MSQL, next_page_id=1, next_table_id=1, free_list_head=-1`，映射区全 0。非空但不足一页或 magic 不匹配时返回 CORRUPT_DATABASE，不覆盖原文件。

## 4. 空闲页管理

- 采用空闲页链表：空闲页的页头 `next_free_page` 指向下一空闲页。
- 分配时从链表头取页；链表空时分配 `next_page_id` 并递增。
- 释放时把页插入链表头，并清零页内容。
- 页 0 不允许释放。

## 5. table_id 与根页映射

- `table_id=0` 保留给系统表 `__catalog`，用户表从 1 开始，由 `next_table_id` 递增分配。
- 每张表的根页（root_page）由 `allocate_page` 独立分配，页号不要求与 table_id 对齐
  （数据页扩展会消耗页号，因此不能用纯函数推导根页）。
- `table_id -> root_page` 的映射显式持久化在页 0 的 root_page 映射区（第 3.2 节）。
- 表根页作为该表数据页链表的入口，数据页通过页头 `next_data_page` 串联。

## 6. 记录编码（RowCodec）

- INT：8 字节有符号大端 `>q`，范围 `[-2^63, 2^63-1]`。
- VARCHAR：2 字节长度前缀 `>H`（按字节计）+ UTF-8 字节。
- bool 不得作为 INT 存储（尽管 bool 是 int 的子类）。
- 记录不跨页；单条记录编码后的最大长度受页容量约束：

```
记录最大长度 = 4096 - 页头(24) - 槽(5) = 4067 字节
```

超出该上限应在记录层（HeapStorage）报 `INVALID_RECORD` 存储错误。

## 7. 持久化与恢复

- 页分配信息（`next_page_id`、`free_list_head`）持久化在页 0 元信息区（DiskPageManager 维护）。
- `next_table_id` 与 root_page 映射区持久化在页 0（HeapStorage 维护）。
- 正常关闭后重新打开：页分配器、空闲链表、表根页映射与记录均完整恢复。
- DELETE 会整理页内记录字节，回收负载空间；存活槽编号不变，中间删除槽置为 offset=0、length=0、flags 包含 SLOT_DELETED，尾部空槽移除。
- INSERT 优先复用删除槽，无空槽时才追加槽；不承诺按插入时间扫描。全部记录删除后，slot_count=0、free_start=24、data_end=4096，页恢复完整可用容量。
- 旧文件中保留原 offset/length 的删除槽仍可读取，页再次插入或删除时统一整理，无需迁移格式。
- 存活 RecordId 不变，已删除 RecordId 不得继续使用，因为槽可能重用。
- 空页不从表页链摘除，供同表复用，不主动缩小文件；DROP TABLE 释放整表页到全局空闲链表。

数据库页格式保持兼容。事务额外使用 minisql.journal 完整前映像日志和 minisql.lock 操作系统锁，恢复同时覆盖元信息、空闲链表、Catalog 与数据页。具体格式及同步顺序见 [事务与恢复](事务与恢复.md)。底层存储接口独立使用时不提供事务保证。
