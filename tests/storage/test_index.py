"""F09 B+ 树索引专项测试：键编码、节点布局、插入分裂、删除合并、范围扫描与重启恢复。"""
from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal
import random

import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, RecordId
from minisql.engine.database import open_database
from minisql.storage.buffer import PageBufferPool
from minisql.storage.file_manager import FileManager
from minisql.storage.index import BTreeIndex, KeyCodec, encode_rid, decode_rid
from minisql.storage.page import (
    HEADER_SIZE, PAGE_TYPE_DATA, DiskPageManager,
    decode_header, decode_meta, encode_header,
)


def _open(path, capacity=64, policy="LRU"):
    pages = DiskPageManager(FileManager(path))
    buffer = PageBufferPool(pages, capacity, policy)
    return pages, buffer


def _columns(*kinds):
    return tuple(ColumnSchema(f"c{i}", kind) for i, kind in enumerate(kinds))


def _assert_ordered(codec, values):
    """编码字节序应等价于 Python 原生排序。"""
    encoded = sorted(codec.encode((v,)) for v in values)
    decoded = [codec.decode(e)[0] for e in encoded]
    assert decoded == sorted(values)


# ---------- KeyCodec ----------

def test_int_key_order():
    codec = KeyCodec(_columns(DataType.INT))
    _assert_ordered(codec, [-(2**63), -100, -1, 0, 1, 100, 2**63 - 1])


def test_varchar_key_order():
    codec = KeyCodec(_columns(DataType.VARCHAR))
    _assert_ordered(codec, ["", "a", "a\x00b", "ab", "b", "中", "中文", "中文a"])


def test_bool_key_order():
    codec = KeyCodec(_columns(DataType.BOOL))
    _assert_ordered(codec, [False, True])


def test_date_key_order():
    codec = KeyCodec(_columns(DataType.DATE))
    _assert_ordered(codec, [date(1, 1, 1), date(1960, 1, 1), date(1970, 1, 1), date(2024, 6, 1), date(9999, 12, 31)])


def test_time_key_order():
    codec = KeyCodec(_columns(DataType.TIME))
    _assert_ordered(codec, [time(0, 0), time(6, 30), time(12, 0, 0, 1), time(23, 59, 59, 999999)])


def test_timestamp_key_order():
    codec = KeyCodec(_columns(DataType.TIMESTAMP))
    _assert_ordered(codec, [datetime(1960, 1, 1), datetime(1970, 1, 1), datetime(2024, 6, 1), datetime(9999, 12, 31, 23, 59, 59, 999999)])


def test_decimal_key_order():
    codec = KeyCodec((ColumnSchema("c0", DataType.DECIMAL, 20, 4),))
    values = [Decimal("-999.99"), Decimal("-1.5"), Decimal("-1.2"), Decimal("-0.001"),
              Decimal("0"), Decimal("0.001"), Decimal("1.15"), Decimal("1.2"), Decimal("1.5"),
              Decimal("999.99")]
    _assert_ordered(codec, values)


def test_null_sorts_before_value():
    codec = KeyCodec(_columns(DataType.INT))
    assert codec.encode((None,)) < codec.encode((-2**63,))


def test_key_roundtrip_all_types():
    codec = KeyCodec(_columns(
        DataType.INT, DataType.VARCHAR, DataType.BOOL, DataType.DECIMAL,
        DataType.DATE, DataType.TIME, DataType.TIMESTAMP))
    values = (42, "中\x00文", True, Decimal("123.45"), date(2024, 1, 2),
              time(13, 14, 15, 123456), datetime(2024, 1, 2, 13, 14, 15, 123456))
    assert codec.decode(codec.encode(values)) == values


def test_key_roundtrip_null():
    codec = KeyCodec(_columns(DataType.INT, DataType.VARCHAR))
    assert codec.decode(codec.encode((None, None))) == (None, None)


def test_composite_key_order():
    codec = KeyCodec(_columns(DataType.INT, DataType.VARCHAR))
    values = [(1, "a"), (1, "b"), (2, "a"), (2, "b"), (3, "")]
    encoded = sorted(codec.encode(tuple(v)) for v in values)
    decoded = [codec.decode(e) for e in encoded]
    assert decoded == sorted(values)


def test_rid_roundtrip():
    rid = RecordId(3, 7)
    assert decode_rid(encode_rid(rid)) == rid


# ---------- BTreeIndex 插入与查找 ----------

def test_create_empty_and_lookup(tmp_path):
    pages, buffer = _open(tmp_path / "db")
    try:
        index = BTreeIndex.create(pages, buffer, _columns(DataType.INT))
        assert index.scan_all() == []
        assert index.lookup((1,)) == []
        assert index.root_page >= 1
    finally:
        buffer.flush_all()
        pages.close()


def test_insert_lookup_single(tmp_path):
    pages, buffer = _open(tmp_path / "db")
    try:
        index = BTreeIndex.create(pages, buffer, _columns(DataType.INT))
        rid = RecordId(1, 0)
        index.insert((5,), rid)
        assert index.lookup((5,)) == [rid]
        assert index.lookup((6,)) == []
        assert index.scan_all() == [rid]
    finally:
        buffer.flush_all()
        pages.close()


def test_insert_many_splits(tmp_path):
    pages, buffer = _open(tmp_path / "db", capacity=2)
    try:
        index = BTreeIndex.create(pages, buffer, _columns(DataType.INT))
        rids = {}
        for i in range(300):
            rid = RecordId(1, i)
            index.insert((i * 7 % 300,), rid)
            rids[rid] = (i * 7 % 300,)
        # 全表按键有序扫描。
        ordered = sorted(rids, key=lambda r: rids[r])
        assert index.scan_all() == ordered
        # 逐个等值查询。
        for i in range(300):
            key = (i * 7 % 300,)
            assert index.lookup(key) == sorted((r for r in rids if rids[r] == key), key=lambda r: r.slot_id)
    finally:
        buffer.flush_all()
        pages.close()


def test_duplicate_keys(tmp_path):
    pages, buffer = _open(tmp_path / "db", capacity=2)
    try:
        index = BTreeIndex.create(pages, buffer, _columns(DataType.INT))
        rids = [RecordId(1, i) for i in range(10)]
        for rid in rids:
            index.insert((7,), rid)
        assert sorted(index.lookup((7,)), key=lambda r: r.slot_id) == rids
        assert index.lookup((8,)) == []
    finally:
        buffer.flush_all()
        pages.close()


def test_range_scan(tmp_path):
    pages, buffer = _open(tmp_path / "db", capacity=2)
    try:
        index = BTreeIndex.create(pages, buffer, _columns(DataType.INT))
        rids = {i: RecordId(1, i) for i in range(100)}
        for i, rid in rids.items():
            index.insert((i,), rid)
        assert index.range_scan((10,), (19,)) == [rids[i] for i in range(10, 20)]
        assert index.range_scan((10,), (19,), True, False) == [rids[i] for i in range(10, 19)]
        assert index.range_scan((10,), (19,), False, True) == [rids[i] for i in range(11, 20)]
        assert index.range_scan(None, (2,)) == [rids[i] for i in range(0, 3)]
        assert index.range_scan((97,), None) == [rids[i] for i in range(97, 100)]
    finally:
        buffer.flush_all()
        pages.close()


def test_composite_key_index(tmp_path):
    pages, buffer = _open(tmp_path / "db", capacity=2)
    try:
        index = BTreeIndex.create(pages, buffer, _columns(DataType.INT, DataType.VARCHAR))
        data = {(i, f"v{i % 5}"): RecordId(1, i) for i in range(80)}
        for key, rid in data.items():
            index.insert(key, rid)
        assert index.lookup((3, "v3")) == [data[(3, "v3")]]
        assert index.scan_all() == [data[k] for k in sorted(data)]
    finally:
        buffer.flush_all()
        pages.close()


def test_null_key_index(tmp_path):
    pages, buffer = _open(tmp_path / "db", capacity=2)
    try:
        index = BTreeIndex.create(pages, buffer, _columns(DataType.INT))
        r1, r2, r3 = RecordId(1, 0), RecordId(1, 1), RecordId(1, 2)
        index.insert((10,), r2)
        index.insert((None,), r1)
        index.insert((5,), r3)
        assert index.lookup((None,)) == [r1]
        assert index.scan_all() == [r1, r3, r2]
    finally:
        buffer.flush_all()
        pages.close()


# ---------- 删除、合并与页回收 ----------

def test_delete_single(tmp_path):
    pages, buffer = _open(tmp_path / "db", capacity=2)
    try:
        index = BTreeIndex.create(pages, buffer, _columns(DataType.INT))
        for i in range(50):
            index.insert((i,), RecordId(1, i))
        index.delete((25,), RecordId(1, 25))
        assert index.lookup((25,)) == []
        assert index.lookup((24,)) == [RecordId(1, 24)]
        assert index.scan_all() == [RecordId(1, i) for i in range(50) if i != 25]
    finally:
        buffer.flush_all()
        pages.close()


def test_delete_many_merges(tmp_path):
    pages, buffer = _open(tmp_path / "db", capacity=2)
    try:
        index = BTreeIndex.create(pages, buffer, _columns(DataType.INT))
        rids = {i: RecordId(1, i) for i in range(500)}
        for i, rid in rids.items():
            index.insert((i,), rid)
        for i in range(0, 500, 2):
            index.delete((i,), rids[i])
        remaining = [rids[i] for i in range(1, 500, 2)]
        assert index.scan_all() == remaining
        for i in range(1, 500, 2):
            assert index.lookup((i,)) == [rids[i]]
    finally:
        buffer.flush_all()
        pages.close()


def test_delete_duplicate_keys(tmp_path):
    pages, buffer = _open(tmp_path / "db", capacity=2)
    try:
        index = BTreeIndex.create(pages, buffer, _columns(DataType.INT))
        rids = [RecordId(1, i) for i in range(20)]
        for rid in rids:
            index.insert((9,), rid)
        index.delete((9,), rids[5])
        assert sorted(index.lookup((9,)), key=lambda r: r.slot_id) == [r for r in rids if r.slot_id != 5]
    finally:
        buffer.flush_all()
        pages.close()


def test_page_recycle_on_full_delete(tmp_path):
    pages, buffer = _open(tmp_path / "db", capacity=2)
    try:
        index = BTreeIndex.create(pages, buffer, _columns(DataType.INT))
        for i in range(500):
            index.insert((i,), RecordId(1, i))
        peak = decode_meta(pages.read_page(0)).next_page_id
        assert peak > 4  # 确实发生了多次分裂。
        for i in range(500):
            index.delete((i,), RecordId(1, i))
        assert index.scan_all() == []
        assert index.root_page >= 1
    finally:
        buffer.flush_all()
        pages.close()


def test_drop_releases_all_index_pages(tmp_path):
    pages, buffer = _open(tmp_path / "db", capacity=2)
    try:
        index = BTreeIndex.create(pages, buffer, _columns(DataType.INT))
        for i in range(500):
            index.insert((i,), RecordId(1, i))
        peak = decode_meta(pages.read_page(0)).next_page_id
        assert peak > 4
        index.drop()
        # 整树页回到空闲链表，next_page_id 不再增长，重建可复用空闲页。
        assert decode_meta(pages.read_page(0)).next_page_id == peak
        rebuilt = BTreeIndex.create(pages, buffer, _columns(DataType.INT))
        assert rebuilt.root_page < peak
    finally:
        buffer.flush_all()
        pages.close()


# ---------- 重启恢复 ----------

def test_reopen_restores_index(tmp_path):
    path = tmp_path / "db"
    pages, buffer = _open(path, capacity=2)
    root = None
    try:
        index = BTreeIndex.create(pages, buffer, _columns(DataType.INT))
        for i in range(200):
            index.insert((i * 3 % 200,), RecordId(1, i))
        root = index.root_page
        buffer.flush_all()
    finally:
        pages.close()

    pages, buffer = _open(path, capacity=2)
    try:
        index = BTreeIndex(pages, buffer, _columns(DataType.INT), root)
        assert len(index.scan_all()) == 200
        for i in range(200):
            assert index.lookup((i * 3 % 200,)) == sorted(
                (RecordId(1, j) for j in range(200) if j * 3 % 200 == i * 3 % 200),
                key=lambda r: r.slot_id)
    finally:
        buffer.flush_all()
        pages.close()


# ---------- 随机一致性 ----------

def test_random_insert_delete_matches_model(tmp_path):
    pages, buffer = _open(tmp_path / "db", capacity=3)
    rng = random.Random(42)
    try:
        index = BTreeIndex.create(pages, buffer, _columns(DataType.INT))
        model = {}  # rid -> key
        for i in range(400):
            if model and rng.random() < 0.5:
                rid = rng.choice(list(model))
                key = model[rid]
                index.delete((key,), rid)
                del model[rid]
            else:
                key = rng.randrange(0, 60)
                rid = RecordId(1, i)
                index.insert((key,), rid)
                model[rid] = key
            # 校验有序扫描。
            assert index.scan_all() == sorted(model, key=lambda r: model[r])
            # 抽查一个等值查询。
            if model:
                probe = rng.choice(list(model.values()))
                expected = sorted((r for r, k in model.items() if k == probe), key=lambda r: r.slot_id)
                assert index.lookup((probe,)) == expected
    finally:
        buffer.flush_all()
        pages.close()


# ---------- 损坏输入 ----------

def test_corrupt_index_page_type_rejected(tmp_path):
    pages, buffer = _open(tmp_path / "db", capacity=2)
    try:
        index = BTreeIndex.create(pages, buffer, _columns(DataType.INT))
        for i in range(100):
            index.insert((i,), RecordId(1, i))
        page = buffer.get_page(index.root_page)
        header = decode_header(page)
        page[:HEADER_SIZE] = encode_header(replace(header, page_type=PAGE_TYPE_DATA))
        buffer.mark_dirty(index.root_page)
        with pytest.raises(MiniSQLError, match="不是索引页"):
            index.scan_all()
    finally:
        buffer.flush_all()
        pages.close()


def test_corrupt_index_entry_length_rejected(tmp_path):
    pages, buffer = _open(tmp_path / "db", capacity=2)
    try:
        index = BTreeIndex.create(pages, buffer, _columns(DataType.INT))
        index.insert((5,), RecordId(1, 0))
        page = buffer.get_page(index.root_page)
        # 叶子页第一个条目长度前缀在 HEADER_SIZE 处，篡改为超大值。
        page[HEADER_SIZE:HEADER_SIZE + 2] = b"\xff\xff"
        buffer.mark_dirty(index.root_page)
        with pytest.raises(MiniSQLError, match="条目内容越界"):
            index.scan_all()
    finally:
        buffer.flush_all()
        pages.close()


def test_corrupt_index_page_id_rejected(tmp_path):
    pages, buffer = _open(tmp_path / "db", capacity=2)
    try:
        index = BTreeIndex.create(pages, buffer, _columns(DataType.INT))
        index.insert((5,), RecordId(1, 0))
        page = buffer.get_page(index.root_page)
        header = decode_header(page)
        page[:HEADER_SIZE] = encode_header(replace(header, page_id=header.page_id + 1))
        buffer.mark_dirty(index.root_page)
        with pytest.raises(MiniSQLError, match="页头编号"):
            index.scan_all()
    finally:
        buffer.flush_all()
        pages.close()


# ---------- 与数据页共同恢复 ----------

def test_index_and_data_recover_together(tmp_path):
    db = open_database(tmp_path)
    try:
        db.execute("CREATE TABLE t(id INT);")
        for i in range(40):
            db.execute(f"INSERT INTO t(id) VALUES ({i});")
        schema = db.catalog.get_table("t")
        rids = {r.row[0]: r.record_id for r in db.storage.scan(schema)}

        # 存量构建索引并提交（模拟 CREATE INDEX 事务）。
        db.execute("BEGIN;")
        index = BTreeIndex.create(db.storage.pages, db.storage.buffer, (schema.columns[0],))
        for key in sorted(rids):
            index.insert((key,), rids[key])
        root = index.root_page
        db.execute("COMMIT;")

        # 后续事务删除数据与索引条目，回滚后两者应一起恢复。
        db.execute("BEGIN;")
        index = BTreeIndex(db.storage.pages, db.storage.buffer, (schema.columns[0],), root)
        db.execute("DELETE FROM t WHERE id = 10;")
        index.delete((10,), rids[10])
        db.execute("ROLLBACK;")

        index = BTreeIndex(db.storage.pages, db.storage.buffer, (schema.columns[0],), root)
        assert index.lookup((10,)) == [rids[10]]
        assert db.execute("SELECT id FROM t WHERE id = 10;")[0].rows == ((10,),)
    finally:
        db.close()
