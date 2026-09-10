"""F06 存储层表结构重写测试：数据重写、物理结构原子替换与失败恢复。

覆盖 HeapStorage.rewrite_table 的增列/删列/改名/改类型、多页重写、失败时旧数据
不变、旧页回收以及关闭重开恢复；不涉及 ALTER SQL 语法与 Catalog 更新（由成员一/三负责）。
"""
from datetime import date, datetime, time
from decimal import Decimal

import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, TableSchema
from minisql.storage.buffer import PageBufferPool
from minisql.storage.file_manager import FileManager
from minisql.storage.page import (
    NO_PAGE, DiskPageManager, decode_header, decode_meta, write_format_version,
)
from minisql.storage.record import HeapStorage


def open_storage(path, capacity=64):
    pages = DiskPageManager(FileManager(path))
    return HeapStorage(pages, PageBufferPool(pages, capacity))


def table(name, columns):
    return TableSchema(name, tuple(columns))


def next_page_id(path):
    """文件当前分配过的页数下界（不随释放回收），用于检测页分配泄漏。"""
    pages = DiskPageManager(FileManager(path))
    meta = decode_meta(pages.read_page(0))
    pages.close()
    return meta.next_page_id


def free_pages(path):
    pages = DiskPageManager(FileManager(path))
    head = decode_meta(pages.read_page(0)).free_list_head
    ids = []
    while head != NO_PAGE:
        ids.append(head)
        head = decode_header(pages.read_page(head)).next_free_page
    pages.close()
    return ids


def make_v1(path):
    """把页 0 的格式版本写回 0，模拟旧库（V1 仅 INT/VARCHAR）。"""
    pages = DiskPageManager(FileManager(path))
    data = bytearray(pages.read_page(0))
    write_format_version(data, 0)
    pages.write_page(0, bytes(data))
    pages.close()


# ---------- 结构变更语义 ----------

def test_rewrite_add_column_with_constant_default(tmp_path):
    path = tmp_path / "db"
    storage = open_storage(path)
    try:
        created = storage.create_table(table("t", [ColumnSchema("id", DataType.INT)]))
        for row in ((1,), (2,), (3,)):
            storage.insert(created, row)
        target = table("t", [ColumnSchema("id", DataType.INT), ColumnSchema("tag", DataType.VARCHAR)])
        target = TableSchema("t", target.columns, table_id=created.table_id)
        result = storage.rewrite_table(created, target, lambda row: (row[0], "d"))
        assert result.table_id == created.table_id
        assert [r.row for r in storage.scan(result)] == [(1, "d"), (2, "d"), (3, "d")]
        storage.flush()
    finally:
        storage.close()

    storage = open_storage(path)
    try:
        assert [r.row for r in storage.scan(result)] == [(1, "d"), (2, "d"), (3, "d")]
    finally:
        storage.close()


def test_rewrite_drop_column(tmp_path):
    storage = open_storage(tmp_path / "db")
    try:
        created = storage.create_table(table("t", [
            ColumnSchema("a", DataType.INT),
            ColumnSchema("b", DataType.VARCHAR),
            ColumnSchema("c", DataType.INT),
        ]))
        storage.insert(created, (1, "x", 10))
        storage.insert(created, (2, "y", 20))
        target = TableSchema("t", (ColumnSchema("a", DataType.INT),
                                   ColumnSchema("c", DataType.INT)), table_id=created.table_id)
        result = storage.rewrite_table(created, target, lambda row: (row[0], row[2]))
        assert [r.row for r in storage.scan(result)] == [(1, 10), (2, 20)]
    finally:
        storage.close()


def test_rewrite_rename_table_and_column(tmp_path):
    storage = open_storage(tmp_path / "db")
    try:
        created = storage.create_table(table("old", [ColumnSchema("a", DataType.INT)]))
        storage.insert(created, (7,))
        target = TableSchema("new", (ColumnSchema("b", DataType.INT),), table_id=created.table_id)
        result = storage.rewrite_table(created, target, lambda row: row)
        assert result.name == "new"
        assert result.columns[0].name == "b"
        assert [r.row for r in storage.scan(result)] == [(7,)]
    finally:
        storage.close()


def test_rewrite_change_type_int_to_varchar(tmp_path):
    path = tmp_path / "db"
    storage = open_storage(path)
    try:
        created = storage.create_table(table("t", [ColumnSchema("i", DataType.INT)]))
        storage.insert(created, (1,))
        storage.insert(created, (-2,))
        target = TableSchema("t", (ColumnSchema("s", DataType.VARCHAR),), table_id=created.table_id)
        result = storage.rewrite_table(created, target, lambda row: (str(row[0]),))
        assert [r.row for r in storage.scan(result)] == [("1",), ("-2",)]
        storage.flush()
    finally:
        storage.close()

    storage = open_storage(path)
    try:
        assert [r.row for r in storage.scan(result)] == [("1",), ("-2",)]
    finally:
        storage.close()


def test_rewrite_transform_may_drop_rows(tmp_path):
    storage = open_storage(tmp_path / "db")
    try:
        created = storage.create_table(table("t", [ColumnSchema("id", DataType.INT)]))
        for row in ((1,), (2,), (3,), (4,)):
            storage.insert(created, row)
        result = storage.rewrite_table(created, created,
                                       lambda row: None if row[0] % 2 else row)
        assert [r.row for r in storage.scan(result)] == [(2,), (4,)]
    finally:
        storage.close()


def test_rewrite_multi_page_preserves_all_rows(tmp_path):
    path = tmp_path / "db"
    storage = open_storage(path, capacity=8)
    try:
        created = storage.create_table(table("t", [ColumnSchema("s", DataType.VARCHAR)]))
        rows = [(f"row-{i:04d}-" + "x" * 100,) for i in range(200)]
        for row in rows:
            storage.insert(created, row)
        target = TableSchema("t", (ColumnSchema("s", DataType.VARCHAR),
                                   ColumnSchema("n", DataType.INT)), table_id=created.table_id)
        result = storage.rewrite_table(
            created, target, lambda row: (row[0], len(row[0])))
        stored = list(storage.scan(result))
        assert len(stored) == 200
        assert {r.row for r in stored} == {(row[0], len(row[0])) for row in rows}
        storage.flush()
    finally:
        storage.close()

    storage = open_storage(path, capacity=8)
    try:
        assert len(list(storage.scan(result))) == 200
    finally:
        storage.close()


def test_rewrite_empty_table(tmp_path):
    storage = open_storage(tmp_path / "db")
    try:
        created = storage.create_table(table("t", [ColumnSchema("id", DataType.INT)]))
        target = TableSchema("t", (ColumnSchema("id", DataType.INT),
                                   ColumnSchema("tag", DataType.VARCHAR)), table_id=created.table_id)
        result = storage.rewrite_table(created, target, lambda row: (row[0], "d"))
        assert list(storage.scan(result)) == []
    finally:
        storage.close()


def test_rewrite_new_types_and_null_roundtrip(tmp_path):
    path = tmp_path / "db"
    storage = open_storage(path)
    try:
        created = storage.create_table(table("t", [ColumnSchema("id", DataType.INT)]))
        storage.insert(created, (1,))
        target = TableSchema("t", tuple([
            ColumnSchema("id", DataType.INT),
            ColumnSchema("b", DataType.BOOL),
            ColumnSchema("d", DataType.DECIMAL, 10, 2),
            ColumnSchema("da", DataType.DATE),
            ColumnSchema("ti", DataType.TIME),
            ColumnSchema("ts", DataType.TIMESTAMP),
        ]), table_id=created.table_id)
        row = (1, True, Decimal("12.34"), date(2024, 5, 6),
               time(7, 8, 9), datetime(2024, 5, 6, 7, 8, 9))
        result = storage.rewrite_table(created, target, lambda old: (old[0],) + row[1:])
        assert [r.row for r in storage.scan(result)] == [row]
        storage.flush()
    finally:
        storage.close()

    storage = open_storage(path)
    try:
        assert [r.row for r in storage.scan(result)] == [row]
    finally:
        storage.close()


# ---------- 失败恢复与物理结构 ----------

def test_rewrite_transform_error_keeps_old_data_and_pages(tmp_path):
    path = tmp_path / "db"
    storage = open_storage(path)
    try:
        created = storage.create_table(table("t", [ColumnSchema("id", DataType.INT)]))
        for row in ((1,), (2,)):
            storage.insert(created, row)
        storage.flush()
    finally:
        storage.close()
    before_pages = next_page_id(path)

    storage = open_storage(path)
    try:
        before_rows = [r.row for r in storage.scan(created)]

        def boom(row):
            raise ValueError("转换失败")
        with pytest.raises(ValueError, match="转换失败"):
            storage.rewrite_table(created, created, boom)
        assert [r.row for r in storage.scan(created)] == before_rows
        storage.flush()
    finally:
        storage.close()

    assert next_page_id(path) == before_pages
    storage = open_storage(path)
    try:
        assert [r.row for r in storage.scan(created)] == [(1,), (2,)]
    finally:
        storage.close()


def test_rewrite_encode_error_keeps_old_data(tmp_path):
    path = tmp_path / "db"
    storage = open_storage(path)
    try:
        created = storage.create_table(table("t", [ColumnSchema("id", DataType.INT)]))
        storage.insert(created, (1,))
        storage.flush()
    finally:
        storage.close()
    before_pages = next_page_id(path)

    storage = open_storage(path)
    try:
        # 目标列数不匹配（transform 返回 2 列，目标只声明 1 列）→ 编码前抛错。
        target = TableSchema("t", (ColumnSchema("a", DataType.INT),), table_id=created.table_id)
        with pytest.raises(MiniSQLError, match="INVALID_RECORD"):
            storage.rewrite_table(created, target, lambda row: (row[0], "多了一列"))
        assert [r.row for r in storage.scan(created)] == [(1,)]
    finally:
        storage.close()

    assert next_page_id(path) == before_pages


def test_rewrite_reclaims_old_page_chain(tmp_path):
    path = tmp_path / "db"
    storage = open_storage(path)
    try:
        created = storage.create_table(table("t", [ColumnSchema("s", DataType.VARCHAR)]))
        for i in range(150):
            storage.insert(created, ("y" * 200,))
        old_root = next(iter(storage.scan(created))).record_id.page_id
        storage.flush()
    finally:
        storage.close()

    storage = open_storage(path)
    try:
        result = storage.rewrite_table(created, created, lambda row: row)
        storage.flush()
    finally:
        storage.close()

    freed = free_pages(path)
    assert old_root in freed
    assert len(freed) >= 1


def test_rewrite_v1_file_rejects_new_types_without_touching_data(tmp_path):
    path = tmp_path / "db"
    pages = DiskPageManager(FileManager(path))
    pages.close()
    make_v1(path)

    storage = open_storage(path)
    try:
        created = storage.create_table(table("t", [ColumnSchema("id", DataType.INT)]))
        storage.insert(created, (5,))
        storage.flush()
    finally:
        storage.close()
    before_pages = next_page_id(path)

    storage = open_storage(path)
    try:
        target = TableSchema("t", (ColumnSchema("id", DataType.INT),
                                   ColumnSchema("b", DataType.BOOL)), table_id=created.table_id)
        with pytest.raises(MiniSQLError, match="需要格式版本 2"):
            storage.rewrite_table(created, target, lambda row: (row[0], True))
        assert [r.row for r in storage.scan(created)] == [(5,)]
    finally:
        storage.close()

    assert next_page_id(path) == before_pages


def test_rewrite_rejects_table_id_mismatch(tmp_path):
    storage = open_storage(tmp_path / "db")
    try:
        created = storage.create_table(table("t", [ColumnSchema("id", DataType.INT)]))
        wrong = TableSchema("t", (ColumnSchema("id", DataType.INT),), table_id=(created.table_id or 0) + 99)
        with pytest.raises(MiniSQLError, match="编号必须与源表一致"):
            storage.rewrite_table(created, wrong, lambda row: row)
    finally:
        storage.close()


def test_rewrite_unknown_table_rejected(tmp_path):
    storage = open_storage(tmp_path / "db")
    try:
        ghost = TableSchema("ghost", (ColumnSchema("id", DataType.INT),), table_id=7)
        with pytest.raises(MiniSQLError, match="UNKNOWN_TABLE"):
            storage.rewrite_table(ghost, ghost, lambda row: row)
    finally:
        storage.close()
