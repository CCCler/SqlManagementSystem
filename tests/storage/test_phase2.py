"""阶段二单元测试：PageBufferPool 与 HeapStorage。"""
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


def _make_storage(path, capacity: int = 64, policy: str = "LRU") -> HeapStorage:
    pages = DiskPageManager(FileManager(path))
    buffer = PageBufferPool(pages, capacity=capacity, policy=policy)
    return HeapStorage(pages, buffer)


def _write_page(pages: DiskPageManager, page_id: int, tag: int) -> None:
    data = bytearray(PAGE_SIZE)
    data[0:4] = struct.pack(">I", tag)
    pages.write_page(page_id, bytes(data))


# ---------- PageBufferPool ----------

def test_buffer_lru_eviction(tmp_path):
    pages = DiskPageManager(FileManager(tmp_path / "test.db"))
    pool = PageBufferPool(pages, capacity=2, policy="LRU")
    p1, p2, p3 = pages.allocate_page(), pages.allocate_page(), pages.allocate_page()
    for p, tag in ((p1, 1), (p2, 2), (p3, 3)):
        _write_page(pages, p, tag)

    pool.get_page(p1)  # miss -> cache [1]
    pool.get_page(p2)  # miss -> cache [1, 2]
    pool.get_page(p1)  # hit, LRU 移到末尾 -> [2, 1]
    pool.get_page(p3)  # miss, 满 -> 淘汰最左 2

    stats = pool.stats()
    assert stats.hits == 1
    assert stats.misses == 3
    assert stats.evictions == 1
    assert pool.replacement_log()[0].page_id == 2
    pages.close()


def test_buffer_fifo_eviction(tmp_path):
    pages = DiskPageManager(FileManager(tmp_path / "test.db"))
    pool = PageBufferPool(pages, capacity=2, policy="FIFO")
    p1, p2, p3 = pages.allocate_page(), pages.allocate_page(), pages.allocate_page()
    for p, tag in ((p1, 1), (p2, 2), (p3, 3)):
        _write_page(pages, p, tag)

    pool.get_page(p1)  # [1]
    pool.get_page(p2)  # [1, 2]
    pool.get_page(p1)  # hit, FIFO 不动 -> [1, 2]
    pool.get_page(p3)  # miss, 满 -> 淘汰最左 1

    assert pool.replacement_log()[0].page_id == 1
    pages.close()


def test_buffer_dirty_eviction_flush(tmp_path):
    pages = DiskPageManager(FileManager(tmp_path / "test.db"))
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


def test_buffer_hit_statistics(tmp_path):
    pages = DiskPageManager(FileManager(tmp_path / "test.db"))
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


# ---------- HeapStorage ----------

def test_heap_create_insert_scan(tmp_path):
    storage = _make_storage(tmp_path / "test.db")
    schema = storage.create_table(_schema())
    assert schema.table_id == 1

    rid = storage.insert(schema, (1, "alice"))
    records = list(storage.scan(schema))
    assert len(records) == 1
    assert records[0].row == (1, "alice")
    assert records[0].record_id == rid
    storage.close()


def test_heap_tables_isolated(tmp_path):
    storage = _make_storage(tmp_path / "test.db")
    s1 = storage.create_table(_schema())
    s2 = storage.create_table(_schema())
    assert (s1.table_id, s2.table_id) == (1, 2)

    storage.insert(s1, (1, "a"))
    storage.insert(s2, (2, "b"))

    assert [r.row for r in storage.scan(s1)] == [(1, "a")]
    assert [r.row for r in storage.scan(s2)] == [(2, "b")]
    storage.close()


def test_heap_cross_page_scan(tmp_path):
    storage = _make_storage(tmp_path / "test.db")
    schema = storage.create_table(_schema())
    for i in range(500):
        storage.insert(schema, (i, f"name{i}"))

    rows = [r.row for r in storage.scan(schema)]
    assert len(rows) == 500
    assert rows[0] == (0, "name0")
    assert rows[-1] == (499, "name499")
    storage.close()


def test_heap_unicode_roundtrip(tmp_path):
    storage = _make_storage(tmp_path / "test.db")
    schema = storage.create_table(_schema())
    storage.insert(schema, (1, "中文测试"))
    assert [r.row for r in storage.scan(schema)] == [(1, "中文测试")]
    storage.close()


def test_heap_delete(tmp_path):
    storage = _make_storage(tmp_path / "test.db")
    schema = storage.create_table(_schema())
    rid1 = storage.insert(schema, (1, "a"))
    storage.insert(schema, (2, "b"))

    storage.delete(schema, rid1)
    assert [r.row for r in storage.scan(schema)] == [(2, "b")]

    with pytest.raises(MiniSQLError) as exc:
        storage.delete(schema, rid1)  # 重复删除
    assert exc.value.code == "INVALID_RECORD"
    storage.close()


def test_heap_oversized_row_rejected(tmp_path):
    storage = _make_storage(tmp_path / "test.db")
    schema = storage.create_table(_schema())
    with pytest.raises(MiniSQLError) as exc:
        storage.insert(schema, (1, "x" * 5000))
    assert exc.value.code == "INVALID_RECORD"
    storage.close()


def test_heap_reopen_restores_records(tmp_path):
    path = tmp_path / "test.db"
    storage = _make_storage(path)
    schema = storage.create_table(_schema())
    storage.insert(schema, (1, "alice"))
    storage.insert(schema, (2, "bob"))
    storage.close()

    # 重新打开，用已分配 table_id 的 schema 恢复扫描
    storage2 = _make_storage(path)
    schema2 = replace(schema, table_id=1)
    assert [r.row for r in storage2.scan(schema2)] == [(1, "alice"), (2, "bob")]
    storage2.close()


def test_heap_create_system_table(tmp_path):
    storage = _make_storage(tmp_path / "test.db")
    catalog = TableSchema(
        "__catalog",
        (ColumnSchema("k", DataType.INT), ColumnSchema("v", DataType.VARCHAR)),
        table_id=0,
    )
    s0 = storage.create_table(catalog)
    assert s0.table_id == 0

    with pytest.raises(MiniSQLError) as exc:
        storage.create_table(catalog)  # 系统表重复创建
    assert exc.value.code == "DUPLICATE_TABLE"
    storage.close()


def test_heap_insert_unknown_table(tmp_path):
    storage = _make_storage(tmp_path / "test.db")
    with pytest.raises(MiniSQLError) as exc:
        storage.insert(replace(_schema(), table_id=99), (1, "x"))
    assert exc.value.code == "UNKNOWN_TABLE"
    storage.close()
