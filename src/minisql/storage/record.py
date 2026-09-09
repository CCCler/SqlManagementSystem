"""记录编解码与堆存储。

RowCodec 负责 INT/VARCHAR 与字节流之间的转换：
    INT      -> 8 字节有符号大端（>q）
    VARCHAR  -> 2 字节长度前缀 + UTF-8 字节（长度按字节计）

HeapStorage 在磁盘上以页为单位存取记录：
    - 每张表有一个根页（root_page），其数据页通过页头 next_data_page 串联。
    - root_page 映射持久化在页 0 的映射区（下标 = table_id）。
"""
import struct
from collections.abc import Iterator
from dataclasses import replace

from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.interfaces import BufferPool, PageManager
from minisql.contracts.models import (
    ColumnSchema, DataType, RecordId, Row, StoredRecord, TableSchema,
)
from minisql.storage.page import (
    HEADER_SIZE, META_OFFSET, META_SIZE, NO_PAGE, NO_ROOT, PAGE_SIZE, PAGE_TYPE_DATA,
    ROOT_MAP_ENTRY_SIZE, ROOT_MAP_OFFSET, ROOT_MAP_STRUCT,
    SLOT_DELETED, SLOT_SIZE, PageHeader, StorageMeta,
    decode_header, decode_meta, decode_slot, encode_header, encode_meta, encode_slot,
)

INT_STRUCT = struct.Struct(">q")
VARCHAR_LEN_STRUCT = struct.Struct(">H")

INT_MIN = -(2**63)
INT_MAX = 2**63 - 1
VARCHAR_MAX_BYTES = 2**16 - 1

# 单条记录编码后的最大字节数：页大小 - 页头 - 一个槽项。
MAX_RECORD_SIZE = PAGE_SIZE - HEADER_SIZE - SLOT_SIZE


class RowCodec:
    def encode(self, schema: TableSchema, row: Row) -> bytes:
        if len(row) != len(schema.columns):
            raise MiniSQLError(
                ErrorStage.STORAGE, "INVALID_RECORD",
                f"列数不匹配：期望 {len(schema.columns)}，实际 {len(row)}",
            )
        parts = [self._encode_value(column, value) for column, value in zip(schema.columns, row)]
        return b"".join(parts)

    def decode(self, schema: TableSchema, data: bytes) -> Row:
        values: list[object] = []
        pos = 0
        for column in schema.columns:
            value, pos = self._decode_value(column, data, pos)
            values.append(value)
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
            if pos + VARCHAR_LEN_STRUCT.size > len(data):
                raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "VARCHAR 长度前缀不足")
            (length,) = VARCHAR_LEN_STRUCT.unpack(data[pos:pos + VARCHAR_LEN_STRUCT.size])
            end = pos + VARCHAR_LEN_STRUCT.size + length
            if end > len(data):
                raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "VARCHAR 数据不足")
            return data[pos + VARCHAR_LEN_STRUCT.size:end].decode("utf-8"), end

        raise MiniSQLError(
            ErrorStage.STORAGE, "TYPE_MISMATCH",
            f"列 {column.name} 不支持类型 {column.data_type.value}",
        )


class HeapStorage:
    def __init__(self, pages: PageManager, buffer: BufferPool) -> None:
        self.pages = pages
        self.buffer = buffer
        self.codec = RowCodec()

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
        page_ids = []
        seen = set()
        while page_id != NO_PAGE:
            if page_id <= 0 or page_id in seen:
                raise MiniSQLError(ErrorStage.STORAGE, "IO_ERROR", "表数据页链表损坏")
            seen.add(page_id)
            header = decode_header(self.buffer.get_page(page_id))
            if header.page_id != page_id or header.page_type != PAGE_TYPE_DATA:
                raise MiniSQLError(ErrorStage.STORAGE, "IO_ERROR", "表数据页类型或编号错误")
            page_ids.append(page_id)
            page_id = header.next_data_page
        self._set_root_page(table_id, NO_ROOT)
        for page_id in page_ids:
            self.buffer.discard_page(page_id)
            self.pages.free_page(page_id)

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
        if header.table_id != table_id:
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
