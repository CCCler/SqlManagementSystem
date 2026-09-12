"""B+ 树索引：键编码、节点页布局与查找/插入/删除。

键编码把每个值转成“保序且自定界”的字节串，使字节字典序等价于 SQL 值排序：
    - NULL 排在所有非 NULL 值之前（单字节 0x00；非 NULL 以 0x01 开头）。
    - INT/DATE/TIMESTAMP 做符号翻转后按无符号比较；TIME 恒非负。
    - VARCHAR 用 0x00 0x00 结尾、0x00 0xff 转义 NUL，保持 UTF-8 字典序。
    - DECIMAL 用“符号 + 整数位数 k + 数字串”编码，负数为补码反转。

叶子节点存 (key, rid)，内部节点存 (key, child)，键均为“列键 + rid”的全键，
保证重复列值也有唯一全键，简化分裂/合并。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.interfaces import BufferPool, PageManager
from minisql.contracts.models import ColumnSchema, DataType, RecordId, Value
from minisql.storage.page import (
    HEADER_SIZE, NO_PAGE, PAGE_SIZE,
    PAGE_TYPE_INDEX_INTERNAL, PAGE_TYPE_INDEX_LEAF,
    PageHeader, decode_header, encode_header,
)

RID_STRUCT = struct.Struct(">II")   # page_id + slot_id，定长 8 字节
CHILD_STRUCT = struct.Struct(">I")  # 子页指针，定长 4 字节
LEN_STRUCT = struct.Struct(">H")    # 节点内条目长度前缀

_INT_FLIP = 1 << 63    # INT/TIMESTAMP 保序偏移
_DATE_FLIP = 1 << 31   # DATE 保序偏移

_EPOCH_DATE = date(1970, 1, 1)
_EPOCH_DATETIME = datetime(1970, 1, 1)

# 节点体（页头之后）的最大字节数；超过即分裂。
MAX_BODY_SIZE = PAGE_SIZE - HEADER_SIZE
MIN_BODY_SIZE = MAX_BODY_SIZE // 2  # 非根节点删除后的下溢阈值（2*MIN <= MAX，合并必能放下）

NULL_MARK = 0x00
VALUE_MARK = 0x01


def _error(code: str, reason: str) -> MiniSQLError:
    return MiniSQLError(ErrorStage.STORAGE, code, reason)


# ---------- 键编码 ----------

class KeyCodec:
    """把一列或多列值编码为保序、自定界的字节串，并支持往返解码。"""

    def __init__(self, columns: tuple[ColumnSchema, ...]) -> None:
        self.columns = columns

    def encode(self, values: tuple[Value, ...]) -> bytes:
        if len(values) != len(self.columns):
            raise _error("INVALID_RECORD", f"键列数不匹配：期望 {len(self.columns)}，实际 {len(values)}")
        parts = []
        for column, value in zip(self.columns, values):
            if value is None:
                parts.append(bytes((NULL_MARK,)))
            else:
                parts.append(bytes((VALUE_MARK,)) + self._encode_value(column, value))
        return b"".join(parts)

    def decode(self, key: bytes) -> tuple[Value, ...]:
        pos = 0
        values: list[Value] = []
        for column in self.columns:
            if pos >= len(key):
                raise _error("INVALID_RECORD", "键数据不足")
            mark = key[pos]
            pos += 1
            if mark == NULL_MARK:
                values.append(None)
            elif mark == VALUE_MARK:
                value, pos = self._decode_value(column, key, pos)
                values.append(value)
            else:
                raise _error("INVALID_RECORD", f"未知键标记 {mark}")
        return tuple(values)

    def _encode_value(self, column: ColumnSchema, value: Value) -> bytes:
        kind = column.data_type
        if kind == DataType.INT:
            if isinstance(value, bool) or not isinstance(value, int):
                raise _error("TYPE_MISMATCH", f"列 {column.name} 期望 INT，实际 {type(value).__name__}")
            return struct.pack(">Q", value + _INT_FLIP)
        if kind == DataType.BOOL:
            if not isinstance(value, bool):
                raise _error("TYPE_MISMATCH", f"列 {column.name} 期望 BOOL，实际 {type(value).__name__}")
            return b"\x00" if not value else b"\x01"
        if kind == DataType.VARCHAR:
            if not isinstance(value, str):
                raise _error("TYPE_MISMATCH", f"列 {column.name} 期望 VARCHAR，实际 {type(value).__name__}")
            return self._encode_str(value)
        if kind == DataType.DECIMAL:
            if not isinstance(value, Decimal):
                raise _error("TYPE_MISMATCH", f"列 {column.name} 期望 DECIMAL，实际 {type(value).__name__}")
            if not value.is_finite():
                raise _error("INVALID_RECORD", f"列 {column.name} 不允许 NaN 或 Infinity")
            return self._encode_decimal(value)
        if kind == DataType.DATE:
            if isinstance(value, datetime) or not isinstance(value, date):
                raise _error("TYPE_MISMATCH", f"列 {column.name} 期望 DATE，实际 {type(value).__name__}")
            days = (value - _EPOCH_DATE).days
            return struct.pack(">I", days + _DATE_FLIP)
        if kind == DataType.TIME:
            if not isinstance(value, time):
                raise _error("TYPE_MISMATCH", f"列 {column.name} 期望 TIME，实际 {type(value).__name__}")
            micros = (((value.hour * 60 + value.minute) * 60 + value.second) * 1_000_000
                      + value.microsecond)
            return struct.pack(">Q", micros)
        if kind == DataType.TIMESTAMP:
            if not isinstance(value, datetime):
                raise _error("TYPE_MISMATCH", f"列 {column.name} 期望 TIMESTAMP，实际 {type(value).__name__}")
            delta = value - _EPOCH_DATETIME
            micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
            return struct.pack(">Q", micros + _INT_FLIP)
        raise _error("TYPE_MISMATCH", f"列 {column.name} 不支持类型 {kind.value}")

    @staticmethod
    def _encode_str(value: str) -> bytes:
        out = bytearray()
        for byte in value.encode("utf-8"):
            if byte == 0x00:
                out += b"\x00\xff"
            else:
                out.append(byte)
        out += b"\x00\x00"
        return bytes(out)

    @staticmethod
    def _encode_decimal(value: Decimal) -> bytes:
        if value == 0:
            return b"\x01"
        sign, digits, exp = value.normalize().as_tuple()
        k = len(digits) + exp  # 小数点前的整数位数（可为负）
        digit_bytes = bytes(0x30 + d for d in digits)
        if sign == 0:  # 正数
            return b"\x02" + struct.pack(">I", k + _DATE_FLIP) + digit_bytes + b"\x00"
        # 负数：反转 k 并补码数字串，使较大数值排在前面（更负）。
        inv_k = struct.pack(">I", _DATE_FLIP - 1 - k)
        comp = bytes(0xFF - b for b in digit_bytes)
        return b"\x00" + inv_k + comp + b"\xff"

    def _decode_value(self, column: ColumnSchema, key: bytes, pos: int) -> tuple[Value, int]:
        kind = column.data_type
        if kind == DataType.INT:
            value = struct.unpack_from(">Q", key, pos)[0] - _INT_FLIP
            return value, pos + 8
        if kind == DataType.BOOL:
            return key[pos] != 0, pos + 1
        if kind == DataType.VARCHAR:
            return self._decode_str(key, pos)
        if kind == DataType.DECIMAL:
            return self._decode_decimal(key, pos)
        if kind == DataType.DATE:
            days = struct.unpack_from(">I", key, pos)[0] - _DATE_FLIP
            return _EPOCH_DATE + timedelta(days=days), pos + 4
        if kind == DataType.TIME:
            micros = struct.unpack_from(">Q", key, pos)[0]
            return (datetime.min + timedelta(microseconds=micros)).time(), pos + 8
        if kind == DataType.TIMESTAMP:
            micros = struct.unpack_from(">Q", key, pos)[0] - _INT_FLIP
            return _EPOCH_DATETIME + timedelta(microseconds=micros), pos + 8
        raise _error("TYPE_MISMATCH", f"列 {column.name} 不支持类型 {kind.value}")

    @staticmethod
    def _decode_str(key: bytes, pos: int) -> tuple[str, int]:
        out = bytearray()
        while True:
            if pos >= len(key):
                raise _error("INVALID_RECORD", "字符串键数据不足")
            byte = key[pos]
            pos += 1
            if byte == 0x00:
                if pos >= len(key):
                    raise _error("INVALID_RECORD", "字符串键转义不完整")
                nxt = key[pos]
                pos += 1
                if nxt == 0x00:
                    break
                if nxt == 0xFF:
                    out.append(0x00)
                else:
                    raise _error("INVALID_RECORD", "字符串键非法转义")
            else:
                out.append(byte)
        return bytes(out).decode("utf-8"), pos

    @staticmethod
    def _decode_decimal(key: bytes, pos: int) -> tuple[Decimal, int]:
        if pos >= len(key):
            raise _error("INVALID_RECORD", "DECIMAL 键数据不足")
        sign = key[pos]
        pos += 1
        if sign == 0x01:
            return Decimal("0"), pos
        if sign == 0x02:
            k = struct.unpack_from(">I", key, pos)[0] - _DATE_FLIP
            pos += 4
            digits: list[int] = []
            while pos < len(key) and key[pos] != 0x00:
                digits.append(key[pos] - 0x30)
                pos += 1
            if pos >= len(key):
                raise _error("INVALID_RECORD", "DECIMAL 键数据不足")
            return Decimal((0, tuple(digits), k - len(digits))), pos + 1
        if sign == 0x00:
            inv_k = struct.unpack_from(">I", key, pos)[0]
            k = _DATE_FLIP - 1 - inv_k
            pos += 4
            digits = []
            while pos < len(key) and key[pos] != 0xFF:
                digits.append(0xCF - key[pos])
                pos += 1
            if pos >= len(key):
                raise _error("INVALID_RECORD", "DECIMAL 键数据不足")
            return Decimal((1, tuple(digits), k - len(digits))), pos + 1
        raise _error("INVALID_RECORD", f"未知 DECIMAL 符号 {sign}")


# ---------- 节点页布局 ----------

@dataclass
class _Node:
    page_id: int
    page_type: int
    entries: list[bytes] = field(default_factory=list)
    child0: int = NO_PAGE       # 内部节点最左子页
    next_leaf: int = NO_PAGE    # 叶子节点后继
    table_id: int = 0

    @property
    def is_leaf(self) -> bool:
        return self.page_type == PAGE_TYPE_INDEX_LEAF

    def body_size(self) -> int:
        base = 0 if self.is_leaf else CHILD_STRUCT.size
        return base + sum(LEN_STRUCT.size + len(entry) for entry in self.entries)


def encode_rid(record_id: RecordId) -> bytes:
    return RID_STRUCT.pack(record_id.page_id, record_id.slot_id)


def decode_rid(data: bytes) -> RecordId:
    page_id, slot_id = RID_STRUCT.unpack(data[-RID_STRUCT.size:])
    return RecordId(page_id, slot_id)


MIN_RID_BYTES = RID_STRUCT.pack(0, 0)
MAX_RID_BYTES = RID_STRUCT.pack(0xFFFFFFFF, 0xFFFFFFFF)


class BTreeIndex:
    """单棵 B+ 树；根页由调用方持久化，本类只读写树页并维护根页号。"""

    def __init__(self, pages: PageManager, buffer: BufferPool,
                 columns: tuple[ColumnSchema, ...], root_page: int,
                 table_id: int = 0) -> None:
        self.pages = pages
        self.buffer = buffer
        self.codec = KeyCodec(columns)
        self.root_page = root_page
        self.table_id = table_id

    @classmethod
    def create(cls, pages: PageManager, buffer: BufferPool,
               columns: tuple[ColumnSchema, ...], table_id: int = 0) -> "BTreeIndex":
        root_page = pages.allocate_page()
        index = cls(pages, buffer, columns, root_page, table_id)
        index._write_fresh_node(_Node(root_page, PAGE_TYPE_INDEX_LEAF, table_id=table_id))
        return index

    # ---------- 节点读写 ----------

    def _encode_node(self, node: _Node) -> bytes:
        header = PageHeader(
            node.page_id, node.page_type, len(node.entries), HEADER_SIZE, PAGE_SIZE,
            node.child0 if node.page_type == PAGE_TYPE_INDEX_INTERNAL else NO_PAGE,
            node.next_leaf if node.is_leaf else NO_PAGE,
            node.table_id,
        )
        data = bytearray(PAGE_SIZE)
        data[:HEADER_SIZE] = encode_header(header)
        pos = HEADER_SIZE
        if node.page_type == PAGE_TYPE_INDEX_INTERNAL:
            data[pos:pos + CHILD_STRUCT.size] = CHILD_STRUCT.pack(node.child0)
            pos += CHILD_STRUCT.size
        for entry in node.entries:
            data[pos:pos + LEN_STRUCT.size] = LEN_STRUCT.pack(len(entry))
            pos += LEN_STRUCT.size
            data[pos:pos + len(entry)] = entry
            pos += len(entry)
        return bytes(data)

    def _decode_node(self, page_id: int) -> _Node:
        page = self.buffer.get_page(page_id)
        header = decode_header(page)
        if header.page_id != page_id:
            raise _error("CORRUPT_DATABASE", f"页 {page_id} 页头编号为 {header.page_id}")
        if header.page_type not in (PAGE_TYPE_INDEX_LEAF, PAGE_TYPE_INDEX_INTERNAL):
            raise _error("CORRUPT_DATABASE", f"页 {page_id} 不是索引页")
        pos = HEADER_SIZE
        child0 = NO_PAGE
        if header.page_type == PAGE_TYPE_INDEX_INTERNAL:
            if pos + CHILD_STRUCT.size > PAGE_SIZE:
                raise _error("CORRUPT_DATABASE", f"页 {page_id} 内部节点缺少最左子页")
            child0 = CHILD_STRUCT.unpack(page[pos:pos + CHILD_STRUCT.size])[0]
            pos += CHILD_STRUCT.size
        entries: list[bytes] = []
        for _ in range(header.slot_count):
            if pos + LEN_STRUCT.size > PAGE_SIZE:
                raise _error("CORRUPT_DATABASE", f"页 {page_id} 条目长度前缀越界")
            (length,) = LEN_STRUCT.unpack(page[pos:pos + LEN_STRUCT.size])
            pos += LEN_STRUCT.size
            if pos + length > PAGE_SIZE:
                raise _error("CORRUPT_DATABASE", f"页 {page_id} 条目内容越界")
            entries.append(bytes(page[pos:pos + length]))
            pos += length
        return _Node(page_id, header.page_type, entries, child0,
                     header.next_data_page, header.table_id)

    def _write_node(self, node: _Node) -> None:
        page = self.buffer.get_page(node.page_id)
        page[:] = self._encode_node(node)
        self.buffer.mark_dirty(node.page_id)

    def _write_fresh_node(self, node: _Node) -> None:
        """写入刚分配、尚未落盘的页（不经缓冲池读取）。"""
        self.pages.write_page(node.page_id, self._encode_node(node))

    def _is_leaf(self, page_id: int) -> bool:
        return decode_header(self.buffer.get_page(page_id)).page_type == PAGE_TYPE_INDEX_LEAF

    def _find_child(self, node: _Node, key: bytes) -> int:
        child = node.child0
        for entry in node.entries:
            if entry[:-CHILD_STRUCT.size] <= key:
                child = CHILD_STRUCT.unpack(entry[-CHILD_STRUCT.size:])[0]
            else:
                break
        return child

    def _find_leaf(self, key: bytes) -> int:
        page_id = self.root_page
        while not self._is_leaf(page_id):
            node = self._decode_node(page_id)
            page_id = self._find_child(node, key)
        return page_id

    def _leftmost_leaf(self) -> int:
        page_id = self.root_page
        while not self._is_leaf(page_id):
            page_id = self._decode_node(page_id).child0
        return page_id

    # ---------- 查找 ----------

    def lookup(self, values: tuple[Value, ...]) -> list[RecordId]:
        key = self.codec.encode(values)
        lo = key + MIN_RID_BYTES
        hi = key + MAX_RID_BYTES
        result: list[RecordId] = []
        page_id = self._find_leaf(lo)
        while page_id != NO_PAGE:
            node = self._decode_node(page_id)
            for entry in node.entries:
                if entry < lo:
                    continue
                if entry > hi:
                    return result
                result.append(decode_rid(entry))
            page_id = node.next_leaf
        return result

    def range_scan(self, lo: tuple[Value, ...] | None,
                   hi: tuple[Value, ...] | None,
                   lo_inclusive: bool = True, hi_inclusive: bool = True) -> list[RecordId]:
        def boundary(values):
            if values is None:
                return None
            if not 1 <= len(values) <= len(self.codec.columns):
                raise _error("INVALID_RECORD", "索引边界必须为非空的最左列前缀")
            return KeyCodec(self.codec.columns[:len(values)]).encode(values)

        lo_key = boundary(lo)
        hi_key = boundary(hi)
        start_key = lo_key + MIN_RID_BYTES if lo_key is not None else None
        result: list[RecordId] = []
        page_id = self._leftmost_leaf() if start_key is None else self._find_leaf(start_key)
        while page_id != NO_PAGE:
            node = self._decode_node(page_id)
            for entry in node.entries:
                entry_key = entry[:-RID_STRUCT.size]
                if lo_key is not None and (entry_key < lo_key or (not lo_inclusive and entry_key.startswith(lo_key))):
                    continue
                if hi_key is not None and ((entry_key > hi_key and not entry_key.startswith(hi_key)) or
                                           (not hi_inclusive and entry_key.startswith(hi_key))):
                    return result
                result.append(decode_rid(entry))
            page_id = node.next_leaf
        return result

    def scan_all(self) -> list[RecordId]:
        result: list[RecordId] = []
        page_id = self._leftmost_leaf()
        while page_id != NO_PAGE:
            node = self._decode_node(page_id)
            result.extend(decode_rid(entry) for entry in node.entries)
            page_id = node.next_leaf
        return result

    # ---------- 插入 ----------

    def insert(self, values: tuple[Value, ...], record_id: RecordId) -> None:
        key = self.codec.encode(values)
        full = key + encode_rid(record_id)
        if len(full) + LEN_STRUCT.size + CHILD_STRUCT.size > MAX_BODY_SIZE:
            raise _error("INVALID_RECORD", "索引键超出单页容量")
        split = self._insert(self.root_page, full)
        if split is not None:
            sep, new_child = split
            new_root = self.pages.allocate_page()
            self._write_fresh_node(_Node(
                new_root, PAGE_TYPE_INDEX_INTERNAL,
                [sep + CHILD_STRUCT.pack(new_child)],
                child0=self.root_page, table_id=self.table_id,
            ))
            self.root_page = new_root

    def _insert(self, page_id: int, full: bytes) -> tuple[bytes, int] | None:
        node = self._decode_node(page_id)
        if node.is_leaf:
            node.entries.append(full)
            node.entries.sort()
            if node.body_size() <= MAX_BODY_SIZE:
                self._write_node(node)
                return None
            return self._split_leaf(node)
        child = self._find_child(node, full)
        split = self._insert(child, full)
        if split is None:
            return None
        sep, new_child = split
        node = self._decode_node(page_id)  # 子页可能分裂，重新读取。
        node.entries.append(sep + CHILD_STRUCT.pack(new_child))
        node.entries.sort()
        if node.body_size() <= MAX_BODY_SIZE:
            self._write_node(node)
            return None
        return self._split_internal(node)

    def _split_leaf(self, node: _Node) -> tuple[bytes, int]:
        left, right = self._split_entries(node.entries)
        old_next = node.next_leaf
        new_page = self.pages.allocate_page()
        node.entries = left
        node.next_leaf = new_page
        self._write_node(node)
        self._write_fresh_node(_Node(new_page, PAGE_TYPE_INDEX_LEAF, right,
                                     next_leaf=old_next, table_id=self.table_id))
        return right[0], new_page

    def _split_internal(self, node: _Node) -> tuple[bytes, int]:
        left, right = self._split_entries(node.entries)
        promoted = right[0]
        new_page = self.pages.allocate_page()
        node.entries = left
        self._write_node(node)
        self._write_fresh_node(_Node(
            new_page, PAGE_TYPE_INDEX_INTERNAL, right[1:],
            child0=CHILD_STRUCT.unpack(promoted[-CHILD_STRUCT.size:])[0],
            table_id=self.table_id,
        ))
        return promoted[:-CHILD_STRUCT.size], new_page

    @staticmethod
    def _split_entries(entries: list[bytes]) -> tuple[list[bytes], list[bytes]]:
        """按字节数尽量均衡地把条目分成两半，保证两半都非空。"""
        total = sum(LEN_STRUCT.size + len(e) for e in entries)
        best_i = 1
        best_diff = total
        acc = 0
        for i in range(1, len(entries)):
            acc += LEN_STRUCT.size + len(entries[i - 1])
            diff = abs(acc - (total - acc))
            if diff < best_diff:
                best_diff = diff
                best_i = i
        return entries[:best_i], entries[best_i:]

    # ---------- 删除 ----------

    def delete(self, values: tuple[Value, ...], record_id: RecordId) -> None:
        full = self.codec.encode(values) + encode_rid(record_id)
        self._delete(self.root_page, full)
        root = self._decode_node(self.root_page)
        if not root.is_leaf and not root.entries:
            # 根内部节点只剩一个子页，塌缩。
            self.root_page = root.child0
            self.buffer.discard_page(root.page_id)
            self.pages.free_page(root.page_id)

    def _delete(self, page_id: int, full: bytes) -> None:
        node = self._decode_node(page_id)
        if node.is_leaf:
            if full in node.entries:
                node.entries.remove(full)
                self._write_node(node)
            return
        child = self._find_child(node, full)
        self._delete(child, full)
        node = self._decode_node(page_id)  # 子页可能因合并/借用改变。
        child_node = self._decode_node(child)
        if child_node.body_size() < MIN_BODY_SIZE:
            self._rebalance(node, child)

    def _rebalance(self, parent: _Node, child_page: int) -> None:
        sibling_page, sibling_left = self._find_sibling(parent, child_page)
        if sibling_page == NO_PAGE:
            return
        child_node = self._decode_node(child_page)
        sibling_node = self._decode_node(sibling_page)
        if sibling_node.body_size() > MIN_BODY_SIZE:
            self._borrow(parent, child_node, sibling_node, sibling_left, child_page, sibling_page)
        else:
            self._merge(parent, child_node, sibling_node, sibling_left, child_page, sibling_page)
        self._write_node(parent)

    def _find_sibling(self, parent: _Node, child_page: int) -> tuple[int, bool]:
        if parent.child0 == child_page:
            sibling = CHILD_STRUCT.unpack(parent.entries[0][-CHILD_STRUCT.size:])[0]
            return sibling, False  # 右兄弟
        for i, entry in enumerate(parent.entries):
            if CHILD_STRUCT.unpack(entry[-CHILD_STRUCT.size:])[0] == child_page:
                left = parent.child0 if i == 0 else CHILD_STRUCT.unpack(
                    parent.entries[i - 1][-CHILD_STRUCT.size:])[0]
                return left, True  # 左兄弟
        raise _error("CORRUPT_DATABASE", "下溢节点不在父节点中")

    def _borrow(self, parent: _Node, child: _Node, sibling: _Node,
                sibling_left: bool, child_page: int, sibling_page: int) -> None:
        if sibling_left:
            self._borrow_from_left(parent, child, sibling, child_page)
        else:
            self._borrow_from_right(parent, child, sibling, sibling_page)
        self._write_node(child)
        self._write_node(sibling)

    def _borrow_from_left(self, parent: _Node, child: _Node, sibling: _Node, child_page: int) -> None:
        if child.is_leaf:
            moved = sibling.entries.pop()
            child.entries.insert(0, moved)
            self._set_parent_sep(parent, child_page, moved)
        else:
            last_sep = sibling.entries[-1][:-CHILD_STRUCT.size]
            last_child = CHILD_STRUCT.unpack(sibling.entries[-1][-CHILD_STRUCT.size:])[0]
            sep_child = self._parent_sep(parent, child_page)
            sibling.entries.pop()
            child.entries.insert(0, sep_child + CHILD_STRUCT.pack(child.child0))
            child.child0 = last_child
            self._set_parent_sep(parent, child_page, last_sep)

    def _borrow_from_right(self, parent: _Node, child: _Node, sibling: _Node, sibling_page: int) -> None:
        if child.is_leaf:
            moved = sibling.entries.pop(0)
            child.entries.append(moved)
            self._set_parent_sep(parent, sibling_page, sibling.entries[0])
        else:
            sep_sibling = self._parent_sep(parent, sibling_page)
            moved_child = sibling.child0
            child.entries.append(sep_sibling + CHILD_STRUCT.pack(moved_child))
            new_child0 = CHILD_STRUCT.unpack(sibling.entries[0][-CHILD_STRUCT.size:])[0]
            new_sep = sibling.entries[0][:-CHILD_STRUCT.size]
            sibling.entries.pop(0)
            sibling.child0 = new_child0
            self._set_parent_sep(parent, sibling_page, new_sep)

    def _merge(self, parent: _Node, child: _Node, sibling: _Node,
               sibling_left: bool, child_page: int, sibling_page: int) -> None:
        if sibling_left:
            self._merge_left(parent, child, sibling, child_page)
            self._write_node(sibling)  # 保留左兄弟
            self.buffer.discard_page(child_page)
            self.pages.free_page(child_page)
        else:
            self._merge_right(parent, child, sibling, sibling_page)
            self._write_node(child)  # 保留左节点
            self.buffer.discard_page(sibling_page)
            self.pages.free_page(sibling_page)

    def _merge_left(self, parent: _Node, child: _Node, sibling: _Node, child_page: int) -> None:
        if child.is_leaf:
            sibling.entries += child.entries
            sibling.entries.sort()
            sibling.next_leaf = child.next_leaf
            self._remove_child(parent, child_page)
        else:
            sep_child = self._parent_sep(parent, child_page)
            sibling.entries.append(sep_child + CHILD_STRUCT.pack(child.child0))
            sibling.entries += child.entries
            sibling.entries.sort()
            self._remove_child(parent, child_page)

    def _merge_right(self, parent: _Node, child: _Node, sibling: _Node, sibling_page: int) -> None:
        if child.is_leaf:
            child.entries += sibling.entries
            child.entries.sort()
            child.next_leaf = sibling.next_leaf
            self._remove_child(parent, sibling_page)
        else:
            sep_sibling = self._parent_sep(parent, sibling_page)
            child.entries.append(sep_sibling + CHILD_STRUCT.pack(sibling.child0))
            child.entries += sibling.entries
            child.entries.sort()
            self._remove_child(parent, sibling_page)

    def _parent_sep(self, parent: _Node, child_page: int) -> bytes:
        for entry in parent.entries:
            if CHILD_STRUCT.unpack(entry[-CHILD_STRUCT.size:])[0] == child_page:
                return entry[:-CHILD_STRUCT.size]
        raise _error("CORRUPT_DATABASE", "子页不在父节点条目中")

    def _set_parent_sep(self, parent: _Node, child_page: int, new_sep: bytes) -> None:
        for i, entry in enumerate(parent.entries):
            if CHILD_STRUCT.unpack(entry[-CHILD_STRUCT.size:])[0] == child_page:
                parent.entries[i] = new_sep + CHILD_STRUCT.pack(child_page)
                parent.entries.sort()
                return
        raise _error("CORRUPT_DATABASE", "子页不在父节点条目中")

    def _remove_child(self, parent: _Node, child_page: int) -> None:
        for i, entry in enumerate(parent.entries):
            if CHILD_STRUCT.unpack(entry[-CHILD_STRUCT.size:])[0] == child_page:
                parent.entries.pop(i)
                return
        raise _error("CORRUPT_DATABASE", "子页不在父节点条目中")

    # ---------- 整树释放 ----------

    def drop(self) -> None:
        """释放整棵索引树占用的所有页，回到空闲链表；调用后本实例不可再用。

        只遍历内部节点的 child0 与各条目子页，叶子由父节点覆盖，不依赖叶子链。
        """
        page_ids: list[int] = []
        self._collect_pages(self.root_page, page_ids)
        for page_id in page_ids:
            self.buffer.discard_page(page_id)
            self.pages.free_page(page_id)

    def _collect_pages(self, page_id: int, out: list[int]) -> None:
        node = self._decode_node(page_id)
        out.append(page_id)
        if node.is_leaf:
            return
        self._collect_pages(node.child0, out)
        for entry in node.entries:
            child = CHILD_STRUCT.unpack(entry[-CHILD_STRUCT.size:])[0]
            self._collect_pages(child, out)
