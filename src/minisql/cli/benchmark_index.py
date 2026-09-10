"""索引与全表扫描的存储层性能对比。

只测量存储层 BTreeIndex 与 HeapStorage.scan，不经过 SQL 编译和事务日志，
因此数据规模、缓存条件与页访问次数可精确控制；耗时仅作参考。

用法：python -m minisql.cli.benchmark_index [N]

指标口径：
- 冷缓存：每次测量重建缓冲池，所有页访问都缺页，磁盘页访问次数 = buffer misses。
- 热缓存：同一缓冲池内连续访问，页已在缓存中，磁盘页访问次数 = 增量 misses。
"""
from __future__ import annotations

from dataclasses import replace
import random
import sys
import tempfile
from pathlib import Path

from minisql.contracts.models import ColumnSchema, DataType, TableSchema
from minisql.storage.buffer import PageBufferPool
from minisql.storage.file_manager import FileManager
from minisql.storage.index import BTreeIndex
from minisql.storage.page import (
    NO_PAGE, ROOT_MAP_ENTRY_SIZE, ROOT_MAP_OFFSET, ROOT_MAP_STRUCT,
    DiskPageManager, decode_header, decode_meta,
)
from minisql.storage.record import HeapStorage

SCHEMA = TableSchema("t", (ColumnSchema("id", DataType.INT),))
PROBES = 200


def _build(path: Path, size: int) -> int:
    """建表、插入 size 行并构建索引，返回索引根页号。"""
    pages = DiskPageManager(FileManager(path))
    buffer = PageBufferPool(pages, 64)
    storage = HeapStorage(pages, buffer)
    schema = storage.create_table(SCHEMA)
    rids = [storage.insert(schema, (i,)) for i in range(size)]
    index = BTreeIndex.create(pages, buffer, (schema.columns[0],))
    for i, rid in enumerate(rids):
        index.insert((i,), rid)
    root = index.root_page
    storage.flush()
    pages.close()
    return root


def _data_page_count(path: Path) -> int:
    """遍历表 1 的数据页链，统计数据页数（不含索引页）。"""
    pages = DiskPageManager(FileManager(path))
    page0 = pages.read_page(0)
    offset = ROOT_MAP_OFFSET + 1 * ROOT_MAP_ENTRY_SIZE
    root = ROOT_MAP_STRUCT.unpack(page0[offset:offset + ROOT_MAP_ENTRY_SIZE])[0]
    count = 0
    page_id = root
    while page_id != NO_PAGE:
        count += 1
        page_id = decode_header(pages.read_page(page_id)).next_data_page
    pages.close()
    return count


def _cold_lookup_misses(path: Path, schema: TableSchema, root: int, keys: list[int]) -> list[int]:
    """每次查找重建缓冲池，统计单次等值查找的磁盘页访问次数。"""
    misses = []
    for key in keys:
        pages = DiskPageManager(FileManager(path))
        buffer = PageBufferPool(pages, 64)
        BTreeIndex(pages, buffer, (schema.columns[0],), root).lookup((key,))
        misses.append(buffer.stats().misses)
        pages.close()
    return misses


def _cold_scan_misses(path: Path, schema: TableSchema, keys: list[int]) -> list[int]:
    """每次扫描重建缓冲池，统计单次全表扫描的磁盘页访问次数。"""
    misses = []
    for key in keys:
        pages = DiskPageManager(FileManager(path))
        buffer = PageBufferPool(pages, 64)
        storage = HeapStorage(pages, buffer)
        _ = [r for r in storage.scan(schema) if r.row[0] == key]
        misses.append(buffer.stats().misses)
        pages.close()
    return misses


def _warm_misses(path: Path, schema: TableSchema, root: int, keys: list[int]) -> tuple[float, float]:
    """同一缓冲池内连续查找，统计每索引/全表扫描的平均增量磁盘页访问。"""
    pages = DiskPageManager(FileManager(path))
    buffer = PageBufferPool(pages, 64)
    index = BTreeIndex(pages, buffer, (schema.columns[0],), root)
    before = buffer.stats().misses
    for key in keys:
        index.lookup((key,))
    index_avg = (buffer.stats().misses - before) / len(keys)

    before = buffer.stats().misses
    storage = HeapStorage(pages, buffer)
    for key in keys:
        _ = [r for r in storage.scan(schema) if r.row[0] == key]
    scan_avg = (buffer.stats().misses - before) / len(keys)
    pages.close()
    return index_avg, scan_avg


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    size = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
    rng = random.Random(42)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "minisql.db"
        root = _build(path, size)
        schema = replace(SCHEMA, table_id=1)
        keys = [rng.randrange(size) for _ in range(PROBES)]

        cold_idx = _cold_lookup_misses(path, schema, root, keys)
        cold_scan = _cold_scan_misses(path, schema, keys)
        warm_idx, warm_scan = _warm_misses(path, schema, root, keys)

        pages = DiskPageManager(FileManager(path))
        total_pages = decode_meta(pages.read_page(0)).next_page_id - 1
        pages.close()
        data_pages = _data_page_count(path)
        index_pages = total_pages - data_pages

        print(f"数据规模 N={size}，索引列 INT；数据页 {data_pages}，索引页 {index_pages}。")
        print("| 数据规模 | 索引等值(冷,miss/次) | 全表扫描(冷,miss/次) | 索引等值(热,miss/次) | 全表扫描(热,miss/次) |")
        print("|---|---|---|---|---|")
        print(f"| {size} | {sum(cold_idx) / len(cold_idx):.1f} "
              f"| {sum(cold_scan) / len(cold_scan):.1f} "
              f"| {warm_idx:.2f} | {warm_scan:.2f} |")


if __name__ == "__main__":
    main()
