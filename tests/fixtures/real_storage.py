"""真实存储夹具：真实文件 + DiskPageManager + HeapStorage，不使用内存替身。

供 F01–F05 的存储形态验证复用：多表共存扫描、变长值、新类型与 NULL 往返、
关闭重开恢复。夹具只调用 minisql.storage 公开接口，不经过 SQL 编译与事务日志，
不承担 SQL 算子实现；需要端到端验证时由调用方改用 open_database。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from minisql.contracts.models import ColumnSchema, DataType, Row, TableSchema
from minisql.storage.buffer import PageBufferPool
from minisql.storage.file_manager import FileManager
from minisql.storage.page import DiskPageManager
from minisql.storage.record import HeapStorage


@dataclass
class RealStore:
    """真实文件上的 HeapStorage 及本次会话已建表的结构快照。"""

    storage: HeapStorage
    path: Path
    schemas: dict[str, TableSchema] = field(default_factory=dict)

    def create(self, name: str, columns) -> TableSchema:
        created = self.storage.create_table(TableSchema(name, tuple(columns)))
        self.schemas[name.lower()] = created
        return created

    def schema(self, name: str) -> TableSchema:
        return self.schemas[name.lower()]

    def insert_rows(self, name: str, rows) -> list:
        schema = self.schema(name)
        return [self.storage.insert(schema, row) for row in rows]

    def rows(self, name: str) -> list[Row]:
        return [record.row for record in self.storage.scan(self.schema(name))]

    def count(self, name: str) -> int:
        return sum(1 for _ in self.storage.scan(self.schema(name)))

    def close(self) -> None:
        self.storage.close()

    def reopen(self, capacity: int = 64, policy: str = "LRU") -> "RealStore":
        """刷新关闭后按同一文件重新打开，保留已建表结构（table_id 由文件恢复）。"""
        self.storage.close()
        fresh = open_real_store(self.path, capacity, policy)
        fresh.schemas = dict(self.schemas)
        return fresh


def open_real_store(path, capacity: int = 64, policy: str = "LRU") -> RealStore:
    """在指定路径打开真实存储；调用方负责 close。"""
    file_path = Path(path)
    pages = DiskPageManager(FileManager(file_path))
    storage = HeapStorage(pages, PageBufferPool(pages, capacity, policy))
    return RealStore(storage, file_path)


# ---------- 常用列与样本行 ----------

def new_type_columns() -> list[ColumnSchema]:
    """覆盖 F08 全部新类型的列定义。"""
    return [
        ColumnSchema("i", DataType.INT),
        ColumnSchema("s", DataType.VARCHAR),
        ColumnSchema("b", DataType.BOOL),
        ColumnSchema("dec", DataType.DECIMAL, 18, 4),
        ColumnSchema("d", DataType.DATE),
        ColumnSchema("t", DataType.TIME),
        ColumnSchema("ts", DataType.TIMESTAMP),
    ]


def full_row() -> Row:
    return (
        42,
        "中文文本",
        True,
        Decimal("123.4567"),
        date(2024, 2, 29),
        time(23, 59, 59, 999999),
        datetime(2024, 2, 29, 23, 59, 59, 999999),
    )


def null_row() -> Row:
    return (None, None, None, None, None, None, None)


def boundary_row() -> Row:
    """各类型边界值：负 INT、空串、假 BOOL、负/零小数、纪元前日期与下界时刻。"""
    return (
        -(2 ** 63),
        "",
        False,
        Decimal("-0.0001"),
        date(1960, 1, 1),
        time(0, 0, 0, 1),
        datetime(1960, 1, 1, 0, 0, 0, 1),
    )


def max_varchar_bytes() -> int:
    """单条 VARCHAR 记录可编码的最大 UTF-8 字节数（页容量 4096，页头 24，槽 5）。"""
    return 4096 - 24 - 5 - 1 - 2
