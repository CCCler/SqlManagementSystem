"""记录编解码与堆存储。

RowCodec 负责 INT/VARCHAR 与字节流之间的转换：
    INT      -> 8 字节有符号大端（>q）
    VARCHAR  -> 2 字节长度前缀 + UTF-8 字节（长度按字节计）

HeapStorage 在磁盘上以页为单位存取记录：
    - 每张表有一个根页（root_page），其数据页通过页头 next_data_page 串联。
    - root_page 映射持久化在页 0 的映射区（下标 = table_id）。
"""
import struct
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.interfaces import BufferPool, PageManager
from minisql.contracts.models import (
    ColumnSchema, DataType, RecordId, Row, StoredRecord, TableSchema,
)
from minisql.storage.page import (
    FORMAT_VERSION_V1, FORMAT_VERSION_V2,
    HEADER_SIZE, META_OFFSET, META_SIZE, NO_PAGE, NO_ROOT, PAGE_SIZE, PAGE_TYPE_DATA,
    ROOT_MAP_ENTRY_SIZE, ROOT_MAP_OFFSET, ROOT_MAP_STRUCT,
    SLOT_DELETED, SLOT_SIZE, PageHeader, StorageMeta,
    decode_header, decode_meta, decode_slot, encode_header, encode_meta, encode_slot,
    read_format_version,
)

INT_STRUCT = struct.Struct(">q")
VARCHAR_LEN_STRUCT = struct.Struct(">H")
BOOL_STRUCT = struct.Struct(">?")
DATE_STRUCT = struct.Struct(">i")       # 距 1970-01-01 的天数（可为负）
TIME_STRUCT = struct.Struct(">q")       # 自午夜起的微秒数
TIMESTAMP_STRUCT = struct.Struct(">q")  # 距 1970-01-01 00:00:00 的微秒数（可为负）

INT_MIN = -(2**63)
INT_MAX = 2**63 - 1
VARCHAR_MAX_BYTES = 2**16 - 1

# V2 格式中每个值前的可空标记：0 表示有值，1 表示 NULL。
VALUE_FLAG = 0x00
NULL_FLAG = 0x01
_V1_TYPES = (DataType.INT, DataType.VARCHAR)

_EPOCH_DATE = date(1970, 1, 1)
_EPOCH_DATETIME = datetime(1970, 1, 1)

# 单条记录编码后的最大字节数：页大小 - 页头 - 一个槽项。
MAX_RECORD_SIZE = PAGE_SIZE - HEADER_SIZE - SLOT_SIZE


class RowCodec:
    """记录编解码，按格式版本分派 V1/V2 编码。

    V1（旧格式）：仅 INT/VARCHAR，无 NULL 标记，保留旧库可读。
    V2（新格式）：每列 1 字节可空标记 + 值，支持全部类型与 NULL。
    """

    def __init__(self, format_version: int = FORMAT_VERSION_V1) -> None:
        self.format_version = format_version

    def encode(self, schema: TableSchema, row: Row) -> bytes:
        if len(row) != len(schema.columns):
            raise MiniSQLError(
                ErrorStage.STORAGE, "INVALID_RECORD",
                f"列数不匹配：期望 {len(schema.columns)}，实际 {len(row)}",
            )
        if self.format_version == FORMAT_VERSION_V1:
            return self._encode_v1(schema, row)
        return self._encode_v2(schema, row)

    def decode(self, schema: TableSchema, data: bytes) -> Row:
        if self.format_version == FORMAT_VERSION_V1:
            return self._decode_v1(schema, data)
        return self._decode_v2(schema, data)

    def _encode_v1(self, schema: TableSchema, row: Row) -> bytes:
        parts = []
        for column, value in zip(schema.columns, row):
            if column.data_type not in _V1_TYPES:
                raise MiniSQLError(
                    ErrorStage.STORAGE, "TYPE_MISMATCH",
                    f"列 {column.name} 类型 {column.data_type.value} 需要格式版本 2",
                )
            if value is None:
                raise MiniSQLError(
                    ErrorStage.STORAGE, "TYPE_MISMATCH",
                    f"列 {column.name} 不支持 NULL（需要格式版本 2）",
                )
            parts.append(self._encode_value(column, value))
        return b"".join(parts)

    def _encode_v2(self, schema: TableSchema, row: Row) -> bytes:
        parts = []
        for column, value in zip(schema.columns, row):
            if value is None:
                parts.append(bytes((NULL_FLAG,)))
            else:
                parts.append(bytes((VALUE_FLAG,)))
                parts.append(self._encode_value(column, value))
        return b"".join(parts)

    def _decode_v1(self, schema: TableSchema, data: bytes) -> Row:
        values: list[object] = []
        pos = 0
        for column in schema.columns:
            if column.data_type not in _V1_TYPES:
                raise MiniSQLError(
                    ErrorStage.STORAGE, "TYPE_MISMATCH",
                    f"列 {column.name} 类型 {column.data_type.value} 需要格式版本 2",
                )
            value, pos = self._decode_value(column, data, pos)
            values.append(value)
        return tuple(values)

    def _decode_v2(self, schema: TableSchema, data: bytes) -> Row:
        values: list[object] = []
        pos = 0
        for column in schema.columns:
            if pos >= len(data):
                raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "可空标记不足")
            flag = data[pos]
            pos += 1
            if flag == NULL_FLAG:
                values.append(None)
            elif flag == VALUE_FLAG:
                value, pos = self._decode_value(column, data, pos)
                values.append(value)
            else:
                raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", f"未知可空标记 {flag}")
        return tuple(values)

    def _encode_value(self, column: ColumnSchema, value: object) -> bytes:
        if column.data_type == DataType.INT:
            # bool 是 int 的子类，但接口约定禁止把 bool 当 INT 存储。
            if isinstance(value, bool) or not isinstance(value, int):
                raise MiniSQLError(
                    ErrorStage.STORAGE, "TYPE_MISMATCH",
                    f"列 {column.name} 期望 INT，实际 {type(value).__name__}",
                )
            if not (INT_MIN <= value <= INT_MAX):
                raise MiniSQLError(
                    ErrorStage.STORAGE, "INVALID_RECORD",
                    f"列 {column.name} 超出 64 位有符号整数范围",
                )
            return INT_STRUCT.pack(value)

        if column.data_type == DataType.VARCHAR:
            if not isinstance(value, str):
                raise MiniSQLError(
                    ErrorStage.STORAGE, "TYPE_MISMATCH",
                    f"列 {column.name} 期望 VARCHAR，实际 {type(value).__name__}",
                )
            raw = value.encode("utf-8")
            if len(raw) > VARCHAR_MAX_BYTES:
                raise MiniSQLError(
                    ErrorStage.STORAGE, "INVALID_RECORD",
                    f"列 {column.name} 字符串过长",
                )
            return VARCHAR_LEN_STRUCT.pack(len(raw)) + raw

        if column.data_type == DataType.BOOL:
            if not isinstance(value, bool):
                raise MiniSQLError(
                    ErrorStage.STORAGE, "TYPE_MISMATCH",
                    f"列 {column.name} 期望 BOOL，实际 {type(value).__name__}",
                )
            return BOOL_STRUCT.pack(value)

        if column.data_type == DataType.DECIMAL:
            if not isinstance(value, Decimal):
                raise MiniSQLError(
                    ErrorStage.STORAGE, "TYPE_MISMATCH",
                    f"列 {column.name} 期望 DECIMAL，实际 {type(value).__name__}",
                )
            if not value.is_finite():
                raise MiniSQLError(
                    ErrorStage.STORAGE, "INVALID_RECORD",
                    f"列 {column.name} 不允许 NaN 或 Infinity",
                )
            raw = str(value).encode("utf-8")
            if len(raw) > VARCHAR_MAX_BYTES:
                raise MiniSQLError(
                    ErrorStage.STORAGE, "INVALID_RECORD",
                    f"列 {column.name} 数值过长",
                )
            return VARCHAR_LEN_STRUCT.pack(len(raw)) + raw

        if column.data_type == DataType.DATE:
            if isinstance(value, datetime) or not isinstance(value, date):
                raise MiniSQLError(
                    ErrorStage.STORAGE, "TYPE_MISMATCH",
                    f"列 {column.name} 期望 DATE，实际 {type(value).__name__}",
                )
            return DATE_STRUCT.pack((value - _EPOCH_DATE).days)

        if column.data_type == DataType.TIME:
            if not isinstance(value, time):
                raise MiniSQLError(
                    ErrorStage.STORAGE, "TYPE_MISMATCH",
                    f"列 {column.name} 期望 TIME，实际 {type(value).__name__}",
                )
            micros = (((value.hour * 60 + value.minute) * 60 + value.second) * 1_000_000
                      + value.microsecond)
            return TIME_STRUCT.pack(micros)

        if column.data_type == DataType.TIMESTAMP:
            if not isinstance(value, datetime):
                raise MiniSQLError(
                    ErrorStage.STORAGE, "TYPE_MISMATCH",
                    f"列 {column.name} 期望 TIMESTAMP，实际 {type(value).__name__}",
                )
            delta = value - _EPOCH_DATETIME
            micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
            return TIMESTAMP_STRUCT.pack(micros)

        raise MiniSQLError(
            ErrorStage.STORAGE, "TYPE_MISMATCH",
            f"列 {column.name} 不支持类型 {column.data_type.value}",
        )

    def _decode_value(self, column: ColumnSchema, data: bytes, pos: int) -> tuple[object, int]:
        if column.data_type == DataType.INT:
            end = pos + INT_STRUCT.size
            if end > len(data):
                raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "INT 数据不足")
            return INT_STRUCT.unpack(data[pos:end])[0], end

        if column.data_type == DataType.VARCHAR:
            return self._decode_varchar(data, pos)

        if column.data_type == DataType.BOOL:
            end = pos + BOOL_STRUCT.size
            if end > len(data):
                raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "BOOL 数据不足")
            return BOOL_STRUCT.unpack(data[pos:end])[0], end

        if column.data_type == DataType.DECIMAL:
            if pos + VARCHAR_LEN_STRUCT.size > len(data):
                raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "DECIMAL 长度前缀不足")
            (length,) = VARCHAR_LEN_STRUCT.unpack(data[pos:pos + VARCHAR_LEN_STRUCT.size])
            end = pos + VARCHAR_LEN_STRUCT.size + length
            if end > len(data):
                raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "DECIMAL 数据不足")
            return Decimal(data[pos + VARCHAR_LEN_STRUCT.size:end].decode("utf-8")), end

        if column.data_type == DataType.DATE:
            end = pos + DATE_STRUCT.size
            if end > len(data):
                raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "DATE 数据不足")
            (days,) = DATE_STRUCT.unpack(data[pos:end])
            return _EPOCH_DATE + timedelta(days=days), end

        if column.data_type == DataType.TIME:
            end = pos + TIME_STRUCT.size
            if end > len(data):
                raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "TIME 数据不足")
            (micros,) = TIME_STRUCT.unpack(data[pos:end])
            return (datetime.min + timedelta(microseconds=micros)).time(), end

        if column.data_type == DataType.TIMESTAMP:
            end = pos + TIMESTAMP_STRUCT.size
            if end > len(data):
                raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "TIMESTAMP 数据不足")
            (micros,) = TIMESTAMP_STRUCT.unpack(data[pos:end])
            return _EPOCH_DATETIME + timedelta(microseconds=micros), end

        raise MiniSQLError(
            ErrorStage.STORAGE, "TYPE_MISMATCH",
            f"列 {column.name} 不支持类型 {column.data_type.value}",
        )

    def _decode_varchar(self, data: bytes, pos: int) -> tuple[str, int]:
        if pos + VARCHAR_LEN_STRUCT.size > len(data):
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "VARCHAR 长度前缀不足")
        (length,) = VARCHAR_LEN_STRUCT.unpack(data[pos:pos + VARCHAR_LEN_STRUCT.size])
        end = pos + VARCHAR_LEN_STRUCT.size + length
        if end > len(data):
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "VARCHAR 数据不足")
        return data[pos + VARCHAR_LEN_STRUCT.size:end].decode("utf-8"), end


class HeapStorage:
    def __init__(self, pages: PageManager, buffer: BufferPool) -> None:
        self.pages = pages
        self.buffer = buffer
        self.format_version = read_format_version(self.pages.read_page(0))
        self.codec = RowCodec(self.format_version)

    # ---------- 页 0 元信息与映射区 ----------

    def _read_meta(self) -> StorageMeta:
        return decode_meta(self.pages.read_page(0))

    def _write_meta(self, meta: StorageMeta) -> None:
        data = bytearray(self.pages.read_page(0))
        data[META_OFFSET:META_OFFSET + META_SIZE] = encode_meta(meta)
        self.pages.write_page(0, bytes(data))

    def _get_root_page(self, table_id: int) -> int:
        data = self.pages.read_page(0)
        offset = ROOT_MAP_OFFSET + table_id * ROOT_MAP_ENTRY_SIZE
        if offset + ROOT_MAP_ENTRY_SIZE > PAGE_SIZE:
            raise MiniSQLError(ErrorStage.STORAGE, "IO_ERROR", f"表编号 {table_id} 超出映射区")
        return ROOT_MAP_STRUCT.unpack(data[offset:offset + ROOT_MAP_ENTRY_SIZE])[0]

    def _set_root_page(self, table_id: int, root_page: int) -> None:
        data = bytearray(self.pages.read_page(0))
        offset = ROOT_MAP_OFFSET + table_id * ROOT_MAP_ENTRY_SIZE
        data[offset:offset + ROOT_MAP_ENTRY_SIZE] = ROOT_MAP_STRUCT.pack(root_page)
        self.pages.write_page(0, bytes(data))

    def _get_insert_hint(self, table_id: int) -> int:
        """读取插入候选页（复用 DATA 根页头 next_free_page 字段，该字段在数据页无意义）。"""
        root_page = self._get_root_page(table_id)
        page = self.buffer.get_page(root_page)
        return decode_header(page).next_free_page

    def _set_insert_hint(self, table_id: int, hint_page: int) -> None:
        """更新插入候选页；值不变时不写，避免每次插入都弄脏根页。"""
        root_page = self._get_root_page(table_id)
        page = self.buffer.get_page(root_page)
        header = decode_header(page)
        if header.next_free_page == hint_page:
            return
        new_header = replace(header, next_free_page=hint_page)
        page[:HEADER_SIZE] = encode_header(new_header)
        self.buffer.mark_dirty(root_page)

    def _require_table_id(self, schema: TableSchema) -> int:
        if schema.table_id is None:
            raise MiniSQLError(ErrorStage.STORAGE, "UNKNOWN_TABLE", schema.name)
        return schema.table_id

    # ---------- 数据页辅助 ----------

    def _init_data_page(self, page_id: int, table_id: int) -> None:
        data = bytearray(PAGE_SIZE)
        header = PageHeader(page_id, PAGE_TYPE_DATA, 0, HEADER_SIZE, PAGE_SIZE, NO_PAGE, NO_PAGE, table_id)
        data[:HEADER_SIZE] = encode_header(header)
        self.pages.write_page(page_id, bytes(data))

    def _slots(self, page: bytearray, header: PageHeader) -> list[tuple[int, int, int]]:
        return [decode_slot(page[start:start + SLOT_SIZE])
                for start in range(HEADER_SIZE, HEADER_SIZE + header.slot_count * SLOT_SIZE, SLOT_SIZE)]

    def _chain_pages(self, root_page: int) -> list[int]:
        """遍历表数据页链，校验编号、类型与环；返回按链表顺序的页号列表。"""
        page_ids: list[int] = []
        seen: set[int] = set()
        page_id = root_page
        while page_id != NO_PAGE:
            if page_id <= 0 or page_id in seen:
                raise MiniSQLError(ErrorStage.STORAGE, "IO_ERROR", "表数据页链表损坏")
            seen.add(page_id)
            header = decode_header(self.buffer.get_page(page_id))
            if header.page_id != page_id or header.page_type != PAGE_TYPE_DATA:
                raise MiniSQLError(ErrorStage.STORAGE, "IO_ERROR", "表数据页类型或编号错误")
            page_ids.append(page_id)
            page_id = header.next_data_page
        return page_ids

    def _compact_page(self, page_id: int, page: bytearray, header: PageHeader,
                      deleted_slot: int | None = None) -> PageHeader:
        """重排记录字节但不重排槽编号；旧删除标记也一并回收。"""
        compacted = bytearray(PAGE_SIZE)
        slots = self._slots(page, header)
        # 仅移除尾部空槽，不移动任何存活槽；全空页恢复完整容量。
        while slots and (len(slots) - 1 == deleted_slot or slots[-1][2] & SLOT_DELETED):
            slots.pop()
        free_start = HEADER_SIZE + len(slots) * SLOT_SIZE
        end = PAGE_SIZE
        for slot_id, (offset, length, flags) in enumerate(slots):
            if slot_id == deleted_slot or flags & SLOT_DELETED:
                slot = encode_slot(0, 0, flags | SLOT_DELETED)
            else:
                if offset < header.free_start or offset + length > PAGE_SIZE or end - length < free_start:
                    raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "记录偏移或长度损坏")
                end -= length
                compacted[end:end + length] = page[offset:offset + length]
                slot = encode_slot(end, length, flags)
            start = HEADER_SIZE + slot_id * SLOT_SIZE
            compacted[start:start + SLOT_SIZE] = slot
        new_header = replace(header, slot_count=len(slots), free_start=free_start, data_end=end)
        compacted[:HEADER_SIZE] = encode_header(new_header)
        page[:] = compacted
        self.buffer.mark_dirty(page_id)
        return new_header

    def _insert_into_page(self, page_id: int, page: bytearray, header: PageHeader,
                          raw: bytes, slot_id: int | None = None) -> RecordId:
        new_slot = slot_id is None
        if new_slot:
            slot_id = header.slot_count
        record_offset = header.data_end - len(raw)
        page[record_offset:record_offset + len(raw)] = raw
        slot_offset = HEADER_SIZE + slot_id * SLOT_SIZE
        page[slot_offset:slot_offset + SLOT_SIZE] = encode_slot(record_offset, len(raw), 0)
        new_header = replace(
            header,
            slot_count=header.slot_count + int(new_slot),
            free_start=header.free_start + (SLOT_SIZE if new_slot else 0),
            data_end=record_offset,
        )
        page[:HEADER_SIZE] = encode_header(new_header)
        self.buffer.mark_dirty(page_id)
        return RecordId(page_id, slot_id)

    # ---------- RecordStorage 接口 ----------

    def create_table(self, schema: TableSchema) -> TableSchema:
        if schema.table_id is not None and schema.table_id != 0:
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "不允许显式指定 table_id")

        if schema.table_id == 0:
            table_id = 0
            if self._get_root_page(0) != NO_ROOT:
                raise MiniSQLError(ErrorStage.STORAGE, "DUPLICATE_TABLE", "__catalog")
        else:
            meta = self._read_meta()
            table_id = meta.next_table_id
            if self._get_root_page(table_id) != NO_ROOT:
                raise MiniSQLError(ErrorStage.STORAGE, "DUPLICATE_TABLE", str(table_id))

        root_page = self.pages.allocate_page()
        self._init_data_page(root_page, table_id)
        self._set_root_page(table_id, root_page)

        if table_id != 0:
            meta = self._read_meta()
            self._write_meta(replace(meta, next_table_id=table_id + 1))

        return replace(schema, table_id=table_id)

    def drop_table(self, schema: TableSchema) -> None:
        table_id = self._require_table_id(schema)
        if table_id == 0 or schema.name.lower() == "__catalog":
            raise MiniSQLError(ErrorStage.STORAGE, "PROTECTED_TABLE", "不能删除系统目录表")
        page_id = self._get_root_page(table_id)
        if page_id == NO_ROOT:
            raise MiniSQLError(ErrorStage.STORAGE, "UNKNOWN_TABLE", schema.name)

        # 先验证完整页链，避免损坏链表导致重复释放；读取缓存中最新的链指针。
        page_ids = self._chain_pages(page_id)
        self._set_root_page(table_id, NO_ROOT)
        for page_id in page_ids:
            self.buffer.discard_page(page_id)
            self.pages.free_page(page_id)

    def rewrite_table(self, schema: TableSchema, new_schema: TableSchema,
                      transform: Callable[[Row], Row | None]) -> TableSchema:
        """把整表存活记录重写为 new_schema 结构，成功后原子替换物理结构。

        transform 对每条旧记录返回新行，返回 None 表示丢弃该记录。整表先完成
        转换与编码，再写入全新页链，最后切换根页映射并回收旧页链；任一步失败都
        保持旧结构与旧数据不变，不会留下半成品页链。

        本方法只提供存储层能力：Catalog 更新与事务原子性由调用方负责（在
        TransactionalDatabase 内调用时，崩溃恢复由整文件前映像日志覆盖）。
        """
        table_id = self._require_table_id(schema)
        if new_schema.table_id != table_id:
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD",
                               "重写目标表编号必须与源表一致")
        if not new_schema.columns:
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "重写目标结构不能没有列")
        old_root = self._get_root_page(table_id)
        if old_root == NO_ROOT:
            raise MiniSQLError(ErrorStage.STORAGE, "UNKNOWN_TABLE", schema.name)

        # 1. 先完成全部转换与编码：类型或容量错误在改动物理结构前暴露。
        encoded: list[bytes] = []
        for record in self.scan(schema):
            new_row = transform(record.row)
            if new_row is None:
                continue
            raw = self.codec.encode(new_schema, new_row)
            if len(raw) > MAX_RECORD_SIZE:
                raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "记录超出单页容量")
            encoded.append(raw)

        # 2. 写入全新页链（尚未切换 table_id 映射）。
        new_root = self._write_chain(table_id, encoded)

        # 3. 切换根页映射；此后读取都走新结构。
        self._set_root_page(table_id, new_root)
        self._set_insert_hint(table_id, new_root)

        # 4. 回收旧页链；失败只泄漏旧页，不影响已切换的新数据。
        for page_id in self._chain_pages(old_root):
            self.buffer.discard_page(page_id)
            self.pages.free_page(page_id)
        return replace(new_schema, table_id=table_id)

    def _write_chain(self, table_id: int, encoded: list[bytes]) -> int:
        """把已编码记录顺序写入一条新页链，返回新根页号；不修改根页映射。"""
        root_page = self.pages.allocate_page()
        self._init_data_page(root_page, table_id)
        page_id = root_page
        for raw in encoded:
            page = self.buffer.get_page(page_id)
            header = decode_header(page)
            if header.data_end - header.free_start < len(raw) + SLOT_SIZE:
                new_page = self.pages.allocate_page()
                self._init_data_page(new_page, table_id)
                page[:HEADER_SIZE] = encode_header(replace(header, next_data_page=new_page))
                self.buffer.mark_dirty(page_id)
                page_id = new_page
                page = self.buffer.get_page(page_id)
                header = decode_header(page)
            self._insert_into_page(page_id, page, header, raw)
        return root_page

    def insert(self, schema: TableSchema, row: Row) -> RecordId:
        table_id = self._require_table_id(schema)
        root_page = self._get_root_page(table_id)
        if root_page == NO_ROOT:
            raise MiniSQLError(ErrorStage.STORAGE, "UNKNOWN_TABLE", schema.name)

        raw = self.codec.encode(schema, row)
        if len(raw) > MAX_RECORD_SIZE:
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "记录超出单页容量")

        # 从插入候选页开始，避免每次从根页线性扫描（消除 O(N²)）。
        hint = self._get_insert_hint(table_id)
        page_id = hint if hint != NO_PAGE else root_page
        while page_id != NO_PAGE:
            page = self.buffer.get_page(page_id)
            header = decode_header(page)
            slots = self._slots(page, header)
            reusable = next((i for i, (_, _, flags) in enumerate(slots) if flags & SLOT_DELETED), None)
            if any(length and flags & SLOT_DELETED for _, length, flags in slots):
                header = self._compact_page(page_id, page, header)
                if reusable is not None and reusable >= header.slot_count:
                    reusable = None
            required = len(raw) + (SLOT_SIZE if reusable is None else 0)
            if header.data_end - header.free_start >= required:
                rid = self._insert_into_page(page_id, page, header, raw, reusable)
                self._set_insert_hint(table_id, page_id)
                return rid

            if header.next_data_page == NO_PAGE:
                # 尾页满，分配新页并追加到链表尾
                new_page_id = self.pages.allocate_page()
                self._init_data_page(new_page_id, table_id)
                tail = replace(header, next_data_page=new_page_id)
                page[:HEADER_SIZE] = encode_header(tail)
                self.buffer.mark_dirty(page_id)
                new_page = self.buffer.get_page(new_page_id)
                rid = self._insert_into_page(new_page_id, new_page, decode_header(new_page), raw)
                self._set_insert_hint(table_id, new_page_id)
                return rid

            page_id = header.next_data_page

        raise MiniSQLError(ErrorStage.STORAGE, "IO_ERROR", "表数据页链表损坏")

    def scan(self, schema: TableSchema) -> Iterator[StoredRecord]:
        table_id = self._require_table_id(schema)
        root_page = self._get_root_page(table_id)
        if root_page == NO_ROOT:
            raise MiniSQLError(ErrorStage.STORAGE, "UNKNOWN_TABLE", schema.name)

        page_id = root_page
        while page_id != NO_PAGE:
            page = self.buffer.get_page(page_id)
            header = decode_header(page)
            next_page = header.next_data_page

            records: list[StoredRecord] = []
            for slot_id in range(header.slot_count):
                slot_offset = HEADER_SIZE + slot_id * SLOT_SIZE
                offset, length, flags = decode_slot(page[slot_offset:slot_offset + SLOT_SIZE])
                if flags & SLOT_DELETED:
                    continue
                row = self.codec.decode(schema, bytes(page[offset:offset + length]))
                records.append(StoredRecord(RecordId(page_id, slot_id), row))

            yield from records
            page_id = next_page

    def fetch(self, schema: TableSchema, record_id: RecordId) -> StoredRecord:
        """索引回表：按 RecordId 读取一条记录，并校验页、槽及表归属。"""
        table_id = self._require_table_id(schema)
        root_page = self._get_root_page(table_id)
        if root_page == NO_ROOT:
            raise MiniSQLError(ErrorStage.STORAGE, "UNKNOWN_TABLE", schema.name)
        if (record_id.page_id <= 0 or record_id.slot_id < 0 or
                record_id.page_id >= self._read_meta().next_page_id):
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", str(record_id))
        page = self.buffer.get_page(record_id.page_id)
        header = decode_header(page)
        if (header.page_id != record_id.page_id or header.page_type != PAGE_TYPE_DATA or
                header.table_id not in (0, table_id)):
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", str(record_id))
        if header.table_id == 0:
            if not self._legacy_page_belongs(root_page, record_id.page_id, table_id):
                raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", str(record_id))
            page = self.buffer.get_page(record_id.page_id)
            header = decode_header(page)
        if record_id.slot_id >= header.slot_count:
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", str(record_id))
        slot_offset = HEADER_SIZE + record_id.slot_id * SLOT_SIZE
        offset, length, flags = decode_slot(page[slot_offset:slot_offset + SLOT_SIZE])
        if (flags & SLOT_DELETED or length <= 0 or
                offset < HEADER_SIZE + header.slot_count * SLOT_SIZE or offset + length > PAGE_SIZE):
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", str(record_id))
        return StoredRecord(record_id, self.codec.decode(schema, bytes(page[offset:offset + length])))

    def _legacy_page_belongs(self, root_page: int, target: int, table_id: int) -> bool:
        """旧页头的 table_id=0 含义不确定，必须从目标表根页验证归属。"""
        page_id = root_page
        seen = set()
        while page_id != NO_PAGE:
            if page_id <= 0 or page_id in seen:
                raise MiniSQLError(ErrorStage.STORAGE, "IO_ERROR", "表数据页链表损坏")
            seen.add(page_id)
            header = decode_header(self.buffer.get_page(page_id))
            if (header.page_id != page_id or header.page_type != PAGE_TYPE_DATA or
                    header.table_id not in (0, table_id)):
                raise MiniSQLError(ErrorStage.STORAGE, "IO_ERROR", "表数据页类型、编号或归属错误")
            if page_id == target:
                return True
            page_id = header.next_data_page
        return False

    def delete(self, schema: TableSchema, record_id: RecordId) -> None:
        table_id = self._require_table_id(schema)
        root_page = self._get_root_page(table_id)
        if root_page == NO_ROOT:
            raise MiniSQLError(ErrorStage.STORAGE, "UNKNOWN_TABLE", schema.name)
        if record_id.page_id <= 0 or record_id.slot_id < 0:
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", str(record_id))

        # 直接按页号定位并校验归属，避免每次从根页线性遍历（消除 O(N)）。
        if record_id.page_id >= self._read_meta().next_page_id:
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", str(record_id))
        page = self.buffer.get_page(record_id.page_id)
        header = decode_header(page)
        if header.page_id != record_id.page_id or header.page_type != PAGE_TYPE_DATA:
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", str(record_id))
        if header.table_id == 0:
            # 0 既可能是系统表，也可能是旧用户页的保留字段，不能直接放行。
            if not self._legacy_page_belongs(root_page, record_id.page_id, table_id):
                raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", str(record_id))
            # 遍历可能淘汰目标页，必须重新取缓存引用。成功删除时一并补写归属。
            page = self.buffer.get_page(record_id.page_id)
            header = replace(decode_header(page), table_id=table_id)
        elif header.table_id != table_id:
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", str(record_id))
        if record_id.slot_id >= header.slot_count:
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", str(record_id))
        slot_offset = HEADER_SIZE + record_id.slot_id * SLOT_SIZE
        _, _, flags = decode_slot(page[slot_offset:slot_offset + SLOT_SIZE])
        if flags & SLOT_DELETED:
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", str(record_id))
        self._compact_page(record_id.page_id, page, header, record_id.slot_id)

        # 目标页在插入候选页之前（或候选页无效）时回拨，保证删除后的空洞可复用。
        hint = self._get_insert_hint(table_id)
        if hint == NO_PAGE or record_id.page_id < hint:
            self._set_insert_hint(table_id, record_id.page_id)

    def flush(self) -> None:
        self.buffer.flush_all()

    def close(self) -> None:
        self.flush()
        self.pages.close()
