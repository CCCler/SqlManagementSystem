"""共享类型：位置从 1 开始，记录按表定义的列顺序存储。"""
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias


class DataType(str, Enum):
    INT = "INT"
    VARCHAR = "VARCHAR"
    BOOL = "BOOL"


Value: TypeAlias = int | str | bool
Row: TypeAlias = tuple[Value, ...]


@dataclass(frozen=True)
class SourcePosition:
    line: int
    column: int


class TokenType(str, Enum):
    KEYWORD = "KEYWORD"
    IDENTIFIER = "IDENTIFIER"
    CONST = "CONST"
    OPERATOR = "OPERATOR"
    DELIMITER = "DELIMITER"
    EOF = "EOF"


@dataclass(frozen=True)
class Token:
    type: TokenType
    lexeme: str
    position: SourcePosition


@dataclass(frozen=True)
class ColumnSchema:
    name: str
    data_type: DataType


@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: tuple[ColumnSchema, ...]
    table_id: int | None = None  # 存储模块在执行建表时分配


@dataclass(frozen=True)
class RecordId:
    page_id: int
    slot_id: int


@dataclass(frozen=True)
class StoredRecord:
    record_id: RecordId
    row: Row


@dataclass(frozen=True)
class ExecutionResult:
    columns: tuple[str, ...] = ()
    rows: tuple[Row, ...] = ()
    affected_rows: int = 0
    message: str = ""


@dataclass(frozen=True)
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0


@dataclass(frozen=True)
class ReplacementEvent:
    page_id: int
    dirty: bool
    policy: str
