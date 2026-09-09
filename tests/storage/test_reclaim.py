"""删除后空间整理、槽复用及旧文件兼容性回归。"""
from dataclasses import replace
import random

import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, RecordId, TableSchema
from minisql.engine.database import open_database
from minisql.storage.buffer import PageBufferPool
from minisql.storage.file_manager import FileManager
from minisql.storage.page import (
    DiskPageManager, HEADER_SIZE, PAGE_SIZE, SLOT_SIZE, SLOT_DELETED,
    decode_header, decode_meta, decode_slot, encode_slot,
)
from minisql.storage.record import HeapStorage, MAX_RECORD_SIZE


SCHEMA = TableSchema("t", (ColumnSchema("value", DataType.VARCHAR),))


def open_storage(path, capacity=64, policy="LRU"):
    pages = DiskPageManager(FileManager(path))
    return HeapStorage(pages, PageBufferPool(pages, capacity, policy))


@pytest.mark.parametrize("deleted_index", [0, 1, 2])
def test_delete_compacts_and_preserves_surviving_ids(tmp_path, deleted_index):
    storage = open_storage(tmp_path / "db")
    try:
        schema = storage.create_table(SCHEMA)
        rows = [("a" * 1200,), ("中文" * 150,), ("c" * 1200,)]
        ids = [storage.insert(schema, row) for row in rows]
        page = storage.buffer.get_page(ids[0].page_id)
        old = decode_header(page)
        storage.delete(schema, ids[deleted_index])
        page = storage.buffer.get_page(ids[0].page_id)
        new = decode_header(page)
        assert new.data_end - old.data_end == len(storage.codec.encode(schema, rows[deleted_index]))
        expected = {rid: row for i, (rid, row) in enumerate(zip(ids, rows)) if i != deleted_index}
        assert {r.record_id: r.row for r in storage.scan(schema)} == expected
        rid = storage.insert(schema, ("z" * 1300,))  # 超出原来的连续空闲空间。
        assert rid == ids[deleted_index]
        assert decode_meta(storage.pages.read_page(0)).next_page_id == 2
        expected[rid] = ("z" * 1300,)
        assert {r.record_id: r.row for r in storage.scan(schema)} == expected
    finally:
        storage.close()


def test_empty_page_and_slot_reuse_do_not_grow_forever(tmp_path):
    storage = open_storage(tmp_path / "db")
    try:
        schema = storage.create_table(SCHEMA)
        anchor = storage.insert(schema, ("anchor",))
        for _ in range(1000):
            rid = storage.insert(schema, ("x",))
            storage.delete(schema, rid)
        assert decode_meta(storage.pages.read_page(0)).next_page_id == 2
        assert list(storage.scan(schema))[0].record_id == anchor
        storage.delete(schema, anchor)
        header = decode_header(storage.buffer.get_page(anchor.page_id))
        assert (header.slot_count, header.free_start, header.data_end) == (0, HEADER_SIZE, PAGE_SIZE)
        largest = ("x" * (MAX_RECORD_SIZE - 2),)
        assert storage.insert(schema, largest).page_id == anchor.page_id
        assert [r.row for r in storage.scan(schema)] == [largest]
    finally:
        storage.close()


@pytest.mark.parametrize("capacity", [1, 64])
@pytest.mark.parametrize("policy", ["LRU", "FIFO"])
def test_cross_page_reuse_after_restart(tmp_path, capacity, policy):
    path = tmp_path / "db"
    storage = open_storage(path, capacity, policy)
    try:
        schema = storage.create_table(SCHEMA)
        ids = [storage.insert(schema, (str(i) * 2500,)) for i in range(3)]
        storage.delete(schema, ids[1])
        storage.flush()
        size = path.stat().st_size
        next_page = decode_meta(storage.pages.read_page(0)).next_page_id
    finally:
        storage.close()
    storage = open_storage(path, capacity, policy)
    try:
        assert {r.record_id for r in storage.scan(schema)} == {ids[0], ids[2]}
        assert storage.insert(schema, ("新" * 900,)).page_id == ids[1].page_id
        storage.flush()
        assert path.stat().st_size == size
        assert decode_meta(storage.pages.read_page(0)).next_page_id == next_page
        assert {r.record_id: r.row for r in storage.scan(schema)}[ids[2]] == ("2" * 2500,)
    finally:
        storage.close()


@pytest.mark.parametrize("deleted_index", [0, 1])
def test_legacy_tombstones_reclaimed_on_insert(tmp_path, deleted_index):
    path = tmp_path / "db"
    storage = open_storage(path)
    try:
        schema = storage.create_table(SCHEMA)
        ids = [storage.insert(schema, ("a" * 1800,)), storage.insert(schema, ("b" * 1800,))]
        page = storage.buffer.get_page(ids[0].page_id)
        start = HEADER_SIZE + deleted_index * SLOT_SIZE
        offset, length, flags = decode_slot(page[start:start + SLOT_SIZE])
        page[start:start + SLOT_SIZE] = encode_slot(offset, length, flags | SLOT_DELETED)
        storage.buffer.mark_dirty(ids[0].page_id)  # 模拟旧版本仅标记、不整理。
    finally:
        storage.close()
    storage = open_storage(path)
    try:
        inserted = storage.insert(schema, ("c" * 1900,))
        assert inserted == ids[deleted_index]
        records = {r.record_id: r.row for r in storage.scan(schema)}
        assert records[ids[1 - deleted_index]] == (("b" if deleted_index == 0 else "a") * 1800,)
        assert records[inserted] == ("c" * 1900,)
        assert decode_meta(storage.pages.read_page(0)).next_page_id == 2
    finally:
        storage.close()


@pytest.mark.parametrize("invalid", ["other_table", "negative_slot", "large_slot", "meta_page", "unknown_page"])
def test_invalid_delete_never_changes_data(tmp_path, invalid):
    storage = open_storage(tmp_path / "db", capacity=1)
    try:
        schema = storage.create_table(SCHEMA)
        other = storage.create_table(replace(SCHEMA, name="other"))
        rid = storage.insert(schema, ("keep",))
        other_rid = storage.insert(other, ("other",))
        bad = {"other_table": other_rid, "negative_slot": RecordId(rid.page_id, -1),
               "large_slot": RecordId(rid.page_id, 999), "meta_page": RecordId(0, 0),
               "unknown_page": RecordId(999, 0)}[invalid]
        with pytest.raises(MiniSQLError, match="INVALID_RECORD"):
            storage.delete(schema, bad)
        assert [r.row for r in storage.scan(schema)] == [("keep",)]
        assert [r.row for r in storage.scan(other)] == [("other",)]
    finally:
        storage.close()


def test_random_insert_delete_matches_live_record_map(tmp_path):
    storage = open_storage(tmp_path / "db", capacity=1)
    rng = random.Random(42)
    expected = {}
    try:
        schema = storage.create_table(SCHEMA)
        for i in range(180):
            if expected and rng.random() < 0.5:
                rid = rng.choice(list(expected))
                storage.delete(schema, rid)
                del expected[rid]
            else:
                row = (str(i) + "中" * rng.randrange(0, 450),)
                rid = storage.insert(schema, row)
                assert rid not in expected
                expected[rid] = row
            assert {r.record_id: r.row for r in storage.scan(schema)} == expected
    finally:
        storage.close()


def test_sql_bulk_delete_reinsert_and_catalog_recovery(tmp_path):
    path = tmp_path / "db"
    db = open_database(path)
    try:
        db.execute("CREATE TABLE t(id INT, name VARCHAR); CREATE TABLE keep(id INT);")
        db.execute("INSERT INTO keep(id) VALUES (99);")
        for i in range(20):
            db.execute(f"INSERT INTO t(id,name) VALUES ({i},'{('中' * 120)}');")
        assert db.execute("DELETE FROM t WHERE id >= 5;")[0].affected_rows == 15
        next_page = decode_meta(db.storage.pages.read_page(0)).next_page_id
        for i in range(5, 20):
            db.execute(f"INSERT INTO t(id,name) VALUES ({i},'{('新' * 120)}');")
        assert decode_meta(db.storage.pages.read_page(0)).next_page_id == next_page
        db.execute("DROP TABLE keep; CREATE TABLE keep(label VARCHAR); INSERT INTO keep(label) VALUES ('new');")
    finally:
        db.close()
    db = open_database(path)
    try:
        assert sorted(db.execute("SELECT id FROM t;")[0].rows) == [(i,) for i in range(20)]
        assert db.execute("SELECT * FROM keep;")[0].rows == (("new",),)
        assert db.execute("DELETE FROM t;")[0].affected_rows == 20
        assert db.execute("SELECT * FROM t;")[0].rows == ()
    finally:
        db.close()


def test_insert_hint_persists_and_rewinds(tmp_path):
    """插入候选页：追加后指向尾页，删除靠前页后回拨，重启后恢复并复用。"""
    path = tmp_path / "db"
    storage = open_storage(path)
    try:
        schema = storage.create_table(SCHEMA)
        ids = [storage.insert(schema, (str(i) * 2500,)) for i in range(3)]
        root = ids[0].page_id
        # 追加后候选页应指向尾页（根页头 next_free_page 复用为候选页）。
        assert decode_header(storage.buffer.get_page(root)).next_free_page == ids[2].page_id
        # 删除中间页后候选页回拨到该页。
        storage.delete(schema, ids[1])
        assert decode_header(storage.buffer.get_page(root)).next_free_page == ids[1].page_id
        storage.flush()
    finally:
        storage.close()
    storage = open_storage(path)
    try:
        rid = storage.insert(schema, ("新" * 900,))
        assert rid.page_id == ids[1].page_id
    finally:
        storage.close()
