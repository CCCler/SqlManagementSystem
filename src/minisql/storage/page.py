"""页格式定义与磁盘页管理器。

页布局（每页 PAGE_SIZE 字节）：
    偏移 0..23    页头 HEADER_STRUCT
    偏移 24..     槽目录（每项 SLOT_SIZE 字节，向下增长）
    ...           空闲空间
    偏移 ..末尾   记录数据（向上增长）

页类型：
    FREE=0 空闲页（页头 next_free_page 串联成空闲链表）
    DATA=1 数据页（含槽目录与记录，next_data_page 串联同表数据页）
    META=2 元信息页（仅页 0，存储全局分配信息）

页 0 元信息页：
    偏移 0..23    页头
    偏移 24..39   元信息区 META_STRUCT（16 字节）
    偏移 40..     root_page 映射区（每项 4 字节，下标 = table_id）
"""
import struct
from dataclasses import dataclass

from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.storage.file_manager import FileManager

PAGE_SIZE = 4096
HEADER_SIZE = 24
SLOT_SIZE = 5
META_SIZE = 16

PAGE_TYPE_FREE = 0
PAGE_TYPE_DATA = 1
PAGE_TYPE_META = 2

NO_PAGE = -1  # 链表空指针 / 尾指针

SLOT_DELETED = 0x01  # 槽标志 bit0：已删除

# page_id(I) + page_type(B) + slot_count(H) + free_start(H) + data_end(H)
#   + next_free_page(i) + next_data_page(i) + reserved(5s)
HEADER_STRUCT = struct.Struct(">IBHHHii5s")
# offset(H) + length(H) + flags(B)
SLOT_STRUCT = struct.Struct(">HHB")
# magic(I) + next_page_id(I) + next_table_id(I) + free_list_head(i)
META_STRUCT = struct.Struct(">IIIi")

META_MAGIC = 0x4D53514C  # "MSQL"
META_OFFSET = HEADER_SIZE

# root_page 映射区：下标 = table_id，值 = 该表根页号；0 表示未分配。
ROOT_MAP_OFFSET = META_OFFSET + META_SIZE  # 40
ROOT_MAP_ENTRY_SIZE = 4
ROOT_MAP_STRUCT = struct.Struct(">I")
NO_ROOT = 0


@dataclass(frozen=True)
class PageHeader:
    page_id: int
    page_type: int
    slot_count: int
    free_start: int
    data_end: int
    next_free_page: int
    next_data_page: int


@dataclass(frozen=True)
class StorageMeta:
    magic: int
    next_page_id: int
    next_table_id: int
    free_list_head: int


def encode_header(header: PageHeader) -> bytes:
    return HEADER_STRUCT.pack(
        header.page_id,
        header.page_type,
        header.slot_count,
        header.free_start,
        header.data_end,
        header.next_free_page,
        header.next_data_page,
        b"\x00" * 5,
    )


def decode_header(data: bytes) -> PageHeader:
    page_id, page_type, slot_count, free_start, data_end, next_free_page, next_data_page, _ = \
        HEADER_STRUCT.unpack(data[:HEADER_SIZE])
    return PageHeader(
        page_id, page_type, slot_count, free_start, data_end,
        next_free_page, next_data_page,
    )


def encode_slot(offset: int, length: int, flags: int = 0) -> bytes:
    return SLOT_STRUCT.pack(offset, length, flags)


def decode_slot(data: bytes) -> tuple[int, int, int]:
    return SLOT_STRUCT.unpack(data)


def encode_meta(meta: StorageMeta) -> bytes:
    return META_STRUCT.pack(meta.magic, meta.next_page_id, meta.next_table_id, meta.free_list_head)


def decode_meta(data: bytes) -> StorageMeta:
    magic, next_page_id, next_table_id, free_list_head = \
        META_STRUCT.unpack(data[META_OFFSET:META_OFFSET + META_SIZE])
    return StorageMeta(magic, next_page_id, next_table_id, free_list_head)


class DiskPageManager:
    """管理 PAGE_SIZE 数据页的分配、释放与读写；页 0 持久化空闲链表等元信息。

    页 0 的元信息区由本类维护 next_page_id / free_list_head；
    root_page 映射区与 next_table_id 由 HeapStorage 维护，本类读写时原样保留。
    """

    def __init__(self, files: FileManager, page_size: int = PAGE_SIZE) -> None:
        self.files = files
        self.page_size = page_size
        self._init_meta_if_needed()

    def _init_meta_if_needed(self) -> None:
        data = self.files.read_at(0, self.page_size)
        if len(data) < self.page_size:
            self._write_fresh_meta_page()
            return
        meta = decode_meta(data)
        if meta.magic != META_MAGIC:
            self._write_fresh_meta_page()

    def _write_fresh_meta_page(self) -> None:
        """全新初始化页 0（仅新文件或 magic 无效时调用）。"""
        data = bytearray(self.page_size)
        header = PageHeader(0, PAGE_TYPE_META, 0, META_OFFSET + META_SIZE, self.page_size, NO_PAGE, NO_PAGE)
        data[:HEADER_SIZE] = encode_header(header)
        data[META_OFFSET:META_OFFSET + META_SIZE] = encode_meta(
            StorageMeta(META_MAGIC, 1, 1, NO_PAGE))
        self.write_page(0, bytes(data))

    def _read_meta(self) -> StorageMeta:
        return decode_meta(self.read_page(0))

    def _write_meta(self, meta: StorageMeta) -> None:
        """更新元信息区，保留页 0 的 root_page 映射区等其他字节。"""
        data = bytearray(self.read_page(0))
        data[META_OFFSET:META_OFFSET + META_SIZE] = encode_meta(meta)
        self.write_page(0, bytes(data))

    def allocate_page(self) -> int:
        meta = self._read_meta()
        if meta.free_list_head != NO_PAGE:
            page_id = meta.free_list_head
            free_header = decode_header(self.read_page(page_id))
            next_head = free_header.next_free_page
            new_meta = StorageMeta(meta.magic, meta.next_page_id, meta.next_table_id, next_head)
        else:
            page_id = meta.next_page_id
            new_meta = StorageMeta(meta.magic, meta.next_page_id + 1, meta.next_table_id, NO_PAGE)
        self._write_meta(new_meta)
        return page_id

    def free_page(self, page_id: int) -> None:
        if page_id <= 0:
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_PAGE", f"不能释放页 {page_id}")
        meta = self._read_meta()
        data = bytearray(self.page_size)
        header = PageHeader(page_id, PAGE_TYPE_FREE, 0, 0, 0, meta.free_list_head, NO_PAGE)
        data[:HEADER_SIZE] = encode_header(header)
        self.write_page(page_id, bytes(data))
        self._write_meta(StorageMeta(meta.magic, meta.next_page_id, meta.next_table_id, page_id))

    def read_page(self, page_id: int) -> bytes:
        data = self.files.read_at(page_id * self.page_size, self.page_size)
        if len(data) != self.page_size:
            raise MiniSQLError(ErrorStage.STORAGE, "IO_ERROR", f"读取页 {page_id} 长度不足")
        return data

    def write_page(self, page_id: int, data: bytes) -> None:
        if len(data) != self.page_size:
            raise MiniSQLError(ErrorStage.STORAGE, "IO_ERROR", f"写入页 {page_id} 长度必须为 {self.page_size}")
        self.files.write_at(page_id * self.page_size, data)

    def close(self) -> None:
        self.files.close()
