"""存储模块验收测试：覆盖 9 个验收场景。"""
import struct
from dataclasses import replace

import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, TableSchema
from minisql.storage.buffer import PageBufferPool
from minisql.storage.file_manager import FileManager
from minisql.storage.page import PAGE_SIZE, DiskPageManager
from minisql.storage.record import HeapStorage


def _schema() -> TableSchema:
    return TableSchema(
        "users",
        (
            ColumnSchema("id", DataType.INT),
            ColumnSchema("name", DataType.VARCHAR),
        ),
    )


def _make_pages(tmp_path) -> DiskPageManager:
    return DiskPageManager(FileManager(tmp_path / "test.db"))


def _make_storage(tmp_path) -> HeapStorage:
    pages = _make_pages(tmp_path)
    buffer = PageBufferPool(pages)
    return HeapStorage(pages, buffer)


def _write_page(pages: DiskPageManager, page_id: int, tag: int) -> None:
    data = bytearray(PAGE_SIZE)
    data[0:4] = struct.pack(">I", tag)
    pages.write_page(page_id, bytes(data))


# ---------- 验收场景实现 ----------

def _allocate_free_reuse_page(tmp_path) -> None:
    pages = _make_pages(tmp_path)
    assert pages.allocate_page() == 1
    assert pages.allocate_page() == 2
    pages.free_page(1)
    assert pages.allocate_page() == 1  # 复用被释放的页
    pages.close()


def _cross_page_scan(tmp_path) -> None:
    storage = _make_storage(tmp_path)
    schema = storage.create_table(_schema())
    for i in range(500):
        storage.insert(schema, (i, f"name{i}"))
    rows = [r.row for r in storage.scan(schema)]
    assert len(rows) == 500
    assert rows[0] == (0, "name0")
    assert rows[-1] == (499, "name499")
    storage.close()


def _unicode_row_roundtrip(tmp_path) -> None:
    storage = _make_storage(tmp_path)
    schema = storage.create_table(_schema())
    storage.insert(schema, (1, "中文测试"))
    assert [r.row for r in storage.scan(schema)] == [(1, "中文测试")]
    storage.close()


def _oversized_row_rejected(tmp_path) -> None:
    storage = _make_storage(tmp_path)
    schema = storage.create_table(_schema())
    with pytest.raises(MiniSQLError) as exc:
        storage.insert(schema, (1, "x" * 5000))
    assert exc.value.code == "INVALID_RECORD"
    storage.close()


def _lru_replacement(tmp_path) -> None:
    pages = _make_pages(tmp_path)
    pool = PageBufferPool(pages, capacity=2, policy="LRU")
    p1, p2, p3 = pages.allocate_page(), pages.allocate_page(), pages.allocate_page()
    for p, tag in ((p1, 1), (p2, 2), (p3, 3)):
        _write_page(pages, p, tag)

    pool.get_page(p1)
    pool.get_page(p2)
    pool.get_page(p1)  # LRU 移到末尾
    pool.get_page(p3)  # 淘汰最久未使用的 p2

    assert pool.replacement_log()[0].page_id == 2
    pages.close()


def _fifo_replacement(tmp_path) -> None:
    pages = _make_pages(tmp_path)
    pool = PageBufferPool(pages, capacity=2, policy="FIFO")
    p1, p2, p3 = pages.allocate_page(), pages.allocate_page(), pages.allocate_page()
    for p, tag in ((p1, 1), (p2, 2), (p3, 3)):
        _write_page(pages, p, tag)

    pool.get_page(p1)
    pool.get_page(p2)
    pool.get_page(p1)  # FIFO 不动
    pool.get_page(p3)  # 淘汰最先插入的 p1

    assert pool.replacement_log()[0].page_id == 1
    pages.close()


def _dirty_eviction_flush(tmp_path) -> None:
    pages = _make_pages(tmp_path)
    pool = PageBufferPool(pages, capacity=1, policy="LRU")
    p1, p2 = pages.allocate_page(), pages.allocate_page()
    _write_page(pages, p1, 0)
    _write_page(pages, p2, 0)

    page = pool.get_page(p1)
    page[0:4] = b"DIRT"
    pool.mark_dirty(p1)
    pool.get_page(p2)  # 淘汰 p1，脏页应先写回

    assert pages.read_page(p1)[0:4] == b"DIRT"
    assert pool.replacement_log()[0].dirty is True
    pages.close()


def _hit_statistics(tmp_path) -> None:
    pages = _make_pages(tmp_path)
    pool = PageBufferPool(pages, capacity=4, policy="LRU")
    p = pages.allocate_page()
    _write_page(pages, p, 0)

    pool.get_page(p)  # miss
    pool.get_page(p)  # hit
    pool.get_page(p)  # hit

    stats = pool.stats()
    assert stats.hits == 2
    assert stats.misses == 1
    assert stats.evictions == 0
    pages.close()


def _reopen_records(tmp_path) -> None:
    path = tmp_path / "test.db"
    storage = _make_storage(tmp_path)
    schema = storage.create_table(_schema())
    storage.insert(schema, (1, "alice"))
    storage.insert(schema, (2, "bob"))
    storage.close()

    pages2 = DiskPageManager(FileManager(path))
    buffer2 = PageBufferPool(pages2)
    storage2 = HeapStorage(pages2, buffer2)
    # 重新打开：table_id 已持久化，恢复扫描
    reopened_schema = replace(schema, table_id=1)
    assert [r.row for r in storage2.scan(reopened_schema)] == [(1, "alice"), (2, "bob")]
    storage2.close()


# ---------- parametrize 验收入口 ----------

_SCENARIOS = {
    "allocate_free_reuse_page": _allocate_free_reuse_page,
    "cross_page_scan": _cross_page_scan,
    "unicode_row_roundtrip": _unicode_row_roundtrip,
    "oversized_row_rejected": _oversized_row_rejected,
    "lru_replacement": _lru_replacement,
    "fifo_replacement": _fifo_replacement,
    "dirty_eviction_flush": _dirty_eviction_flush,
    "hit_statistics": _hit_statistics,
    "reopen_records": _reopen_records,
}


@pytest.mark.parametrize("scenario", list(_SCENARIOS))
def test_acceptance(scenario, tmp_path):
    _SCENARIOS[scenario](tmp_path)
