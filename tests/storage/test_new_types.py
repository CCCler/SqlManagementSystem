"""F08 存储专项测试：NULL、新类型编解码、BOOL/INT 严格区分与 V1/V2 兼容。

覆盖 RowCodec 的 V2 编码往返、可空标记、DECIMAL/DATE/TIME/TIMESTAMP 边界，
以及 HeapStorage 对新类型和 NULL 的持久化、跨页与重启恢复。
"""
from datetime import date, datetime, time
from decimal import Decimal

import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, TableSchema
from minisql.storage.buffer import PageBufferPool
from minisql.storage.file_manager import FileManager
from minisql.storage.page import (
    FORMAT_VERSION_V1, FORMAT_VERSION_V2, PAGE_SIZE,
    DiskPageManager, read_format_version, write_format_version,
)
from minisql.storage.record import HeapStorage, RowCodec


def _open_storage(path, capacity=64, policy="LRU"):
    pages = DiskPageManager(FileManager(path))
    return HeapStorage(pages, PageBufferPool(pages, capacity, policy))


def _all_types_schema() -> TableSchema:
    return TableSchema(
        "t",
        (
            ColumnSchema("i", DataType.INT),
            ColumnSchema("s", DataType.VARCHAR),
            ColumnSchema("b", DataType.BOOL),
            ColumnSchema("d", DataType.DECIMAL, 10, 2),
            ColumnSchema("da", DataType.DATE),
            ColumnSchema("ti", DataType.TIME),
            ColumnSchema("ts", DataType.TIMESTAMP),
        ),
    )


# ---------- RowCodec：V2 新类型与 NULL 编解码 ----------

def test_v2_codec_roundtrip_all_types():
    codec = RowCodec(FORMAT_VERSION_V2)
    schema = _all_types_schema()
    row = (
        42,
        "中文文本",
        True,
        Decimal("123.45"),
        date(2024, 1, 2),
        time(13, 14, 15, 123456),
        datetime(2024, 1, 2, 13, 14, 15, 123456),
    )
    assert codec.decode(schema, codec.encode(schema, row)) == row


def test_v2_codec_roundtrip_null():
    codec = RowCodec(FORMAT_VERSION_V2)
    schema = _all_types_schema()
    row = (None, None, None, None, None, None, None)
    assert codec.decode(schema, codec.encode(schema, row)) == row


def test_v2_codec_mixed_null_and_values():
    codec = RowCodec(FORMAT_VERSION_V2)
    schema = _all_types_schema()
    row = (None, "仅此列有值", None, None, None, None, None)
    assert codec.decode(schema, codec.encode(schema, row)) == row


def test_v2_codec_unknown_null_flag_rejected():
    codec = RowCodec(FORMAT_VERSION_V2)
    schema = _all_types_schema()
    # 第二列的可空标记置为非法值 0x02。
    raw = bytearray(codec.encode(schema, (1, "x", True, Decimal("1"), date(2024, 1, 1), time(), datetime(2024, 1, 1))))
    raw[9] = 0x02  # 第 0 列 1 字节标记 + 8 字节 INT 后，第 1 列 VARCHAR 的可空标记。
    with pytest.raises(MiniSQLError, match="未知可空标记"):
        codec.decode(schema, bytes(raw))


def test_v2_bool_column_rejects_int():
    codec = RowCodec(FORMAT_VERSION_V2)
    schema = TableSchema("t", (ColumnSchema("b", DataType.BOOL),))
    with pytest.raises(MiniSQLError, match="TYPE_MISMATCH"):
        codec.encode(schema, (1,))


def test_v2_int_column_rejects_bool():
    codec = RowCodec(FORMAT_VERSION_V2)
    schema = TableSchema("t", (ColumnSchema("i", DataType.INT),))
    with pytest.raises(MiniSQLError, match="TYPE_MISMATCH"):
        codec.encode(schema, (True,))


@pytest.mark.parametrize("value", [Decimal("0"), Decimal("-0.001"), Decimal("1234567890.1234")])
def test_v2_decimal_roundtrip(value):
    codec = RowCodec(FORMAT_VERSION_V2)
    schema = TableSchema("t", (ColumnSchema("d", DataType.DECIMAL, 20, 4),))
    assert codec.decode(schema, codec.encode(schema, (value,))) == (value,)


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_v2_decimal_rejects_non_finite(bad):
    codec = RowCodec(FORMAT_VERSION_V2)
    schema = TableSchema("t", (ColumnSchema("d", DataType.DECIMAL, 10, 2),))
    with pytest.raises(MiniSQLError, match="INVALID_RECORD"):
        codec.encode(schema, (bad,))


def test_v2_date_time_timestamp_negative_boundaries():
    codec = RowCodec(FORMAT_VERSION_V2)
    schema = TableSchema("t", (
        ColumnSchema("da", DataType.DATE),
        ColumnSchema("ti", DataType.TIME),
        ColumnSchema("ts", DataType.TIMESTAMP),
    ))
    row = (date(1960, 1, 1), time(0, 0, 0, 1), datetime(1960, 1, 1, 0, 0, 0, 1))
    assert codec.decode(schema, codec.encode(schema, row)) == row


def test_v2_date_time_timestamp_upper_boundaries():
    codec = RowCodec(FORMAT_VERSION_V2)
    schema = TableSchema("t", (
        ColumnSchema("da", DataType.DATE),
        ColumnSchema("ti", DataType.TIME),
        ColumnSchema("ts", DataType.TIMESTAMP),
    ))
    row = (date(9999, 12, 31), time(23, 59, 59, 999999), datetime(9999, 12, 31, 23, 59, 59, 999999))
    assert codec.decode(schema, codec.encode(schema, row)) == row


def test_v2_date_column_rejects_datetime():
    codec = RowCodec(FORMAT_VERSION_V2)
    schema = TableSchema("t", (ColumnSchema("da", DataType.DATE),))
    with pytest.raises(MiniSQLError, match="TYPE_MISMATCH"):
        codec.encode(schema, (datetime(2024, 1, 1),))


def test_v2_timestamp_column_rejects_date():
    codec = RowCodec(FORMAT_VERSION_V2)
    schema = TableSchema("t", (ColumnSchema("ts", DataType.TIMESTAMP),))
    with pytest.raises(MiniSQLError, match="TYPE_MISMATCH"):
        codec.encode(schema, (date(2024, 1, 1),))


# ---------- 格式版本识别与 V1 兼容 ----------

def test_read_format_version_normalizes_zero_to_v1():
    page0 = bytearray(PAGE_SIZE)
    assert read_format_version(bytes(page0)) == FORMAT_VERSION_V1
    write_format_version(page0, FORMAT_VERSION_V2)
    assert read_format_version(bytes(page0)) == FORMAT_VERSION_V2


def test_fresh_file_uses_v2(tmp_path):
    storage = _open_storage(tmp_path / "db")
    try:
        assert storage.format_version == FORMAT_VERSION_V2
    finally:
        storage.close()


def test_v1_codec_rejects_new_types_and_null():
    codec = RowCodec()  # 默认 V1。
    for column in (ColumnSchema("b", DataType.BOOL), ColumnSchema("d", DataType.DECIMAL, 10, 2),
                   ColumnSchema("da", DataType.DATE), ColumnSchema("ti", DataType.TIME),
                   ColumnSchema("ts", DataType.TIMESTAMP)):
        schema = TableSchema("t", (column,))
        with pytest.raises(MiniSQLError, match="需要格式版本 2"):
            codec.encode(schema, (None,))
    # V1 下 INT/VARCHAR 依然可用，但显式 NULL 被拒绝。
    schema = TableSchema("t", (ColumnSchema("i", DataType.INT), ColumnSchema("s", DataType.VARCHAR)))
    assert codec.decode(schema, codec.encode(schema, (1, "x"))) == (1, "x")
    with pytest.raises(MiniSQLError, match="不支持 NULL"):
        codec.encode(schema, (1, None))


def test_v1_file_detected_and_readable_on_reopen(tmp_path):
    """旧文件页 0 的 reserved 字节为 0，重开后归一化为 V1 并可继续读写 INT/VARCHAR。"""
    path = tmp_path / "db"
    pages = DiskPageManager(FileManager(path))
    data = bytearray(pages.read_page(0))
    write_format_version(data, 0)  # 模拟旧格式（保留字节为 0）。
    pages.write_page(0, bytes(data))
    pages.close()

    storage = _open_storage(path)
    try:
        assert storage.format_version == FORMAT_VERSION_V1
        schema = storage.create_table(TableSchema("t", (ColumnSchema("i", DataType.INT),
                                                        ColumnSchema("s", DataType.VARCHAR))))
        storage.insert(schema, (7, "旧数据"))
        assert [r.row for r in storage.scan(schema)] == [(7, "旧数据")]
    finally:
        storage.close()


# ---------- HeapStorage：新类型与 NULL 持久化、重启 ----------

def test_heap_new_types_roundtrip_and_restart(tmp_path):
    path = tmp_path / "db"
    schema = _all_types_schema()
    rows = [
        (1, "一", True, Decimal("1.10"), date(2024, 2, 29), time(8, 30), datetime(2024, 2, 29, 8, 30)),
        (2, None, False, None, None, None, None),
        (3, "三", True, Decimal("-99.99"), date(1960, 6, 1), time(23, 59, 59, 999999), datetime(1960, 6, 1)),
    ]
    storage = _open_storage(path)
    try:
        created = storage.create_table(schema)
        for row in rows:
            storage.insert(created, row)
        assert [r.row for r in storage.scan(created)] == rows
        storage.flush()
    finally:
        storage.close()

    storage = _open_storage(path)
    try:
        assert [r.row for r in storage.scan(created)] == rows
    finally:
        storage.close()


def test_heap_cross_page_new_types(tmp_path):
    storage = _open_storage(tmp_path / "db")
    try:
        schema = storage.create_table(TableSchema("t", (ColumnSchema("s", DataType.VARCHAR),)))
        rows = [(f"{i}" * 2500,) for i in range(3)]
        for row in rows:
            storage.insert(schema, row)
        assert {r.row for r in storage.scan(schema)} == set(rows)
    finally:
        storage.close()
