from collections.abc import Iterator
from minisql.contracts.interfaces import BufferPool, PageManager
from minisql.contracts.models import RecordId, Row, StoredRecord, TableSchema


class RowCodec:
    def encode(self, schema: TableSchema, row: Row) -> bytes:
        raise NotImplementedError("成员二：记录序列化")

    def decode(self, schema: TableSchema, data: bytes) -> Row:
        raise NotImplementedError("成员二：记录反序列化")


class HeapStorage:
    def __init__(self, pages: PageManager, buffer: BufferPool) -> None:
        self.pages = pages
        self.buffer = buffer

    def create_table(self, schema: TableSchema) -> TableSchema:
        raise NotImplementedError("成员二：分配稳定 table_id 与表数据页")

    def insert(self, schema: TableSchema, row: Row) -> RecordId:
        raise NotImplementedError("成员二：插入记录")

    def scan(self, schema: TableSchema) -> Iterator[StoredRecord]:
        raise NotImplementedError("成员二：逐页扫描并保留 RecordId")

    def delete(self, schema: TableSchema, record_id: RecordId) -> None:
        raise NotImplementedError("成员二：删除记录并维护空闲空间")

    def flush(self) -> None:
        raise NotImplementedError("成员二：持久化数据与页分配元信息")

    def close(self) -> None:
        raise NotImplementedError("成员二：刷新并关闭存储")
