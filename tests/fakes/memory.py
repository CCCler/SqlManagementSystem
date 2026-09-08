from collections.abc import Iterator
from dataclasses import replace
from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.models import RecordId, Row, StoredRecord, TableSchema


class MemoryCatalog:
    """测试替身：仅模拟目录查询和注册，不持久化。"""

    def __init__(self, schemas: tuple[TableSchema, ...] = ()) -> None:
        self.tables: dict[str, TableSchema] = {}
        for schema in schemas:
            self.register_table(schema)

    def bootstrap(self) -> None:
        pass  # 内存替身无磁盘初始化

    def get_table(self, name: str) -> TableSchema | None:
        return self.tables.get(name.lower())

    def list_tables(self) -> tuple[TableSchema, ...]:
        return tuple(s for s in self.tables.values() if s.table_id != 0)

    def register_table(self, schema: TableSchema) -> None:
        key = schema.name.lower()
        if key in self.tables:
            raise MiniSQLError(ErrorStage.SEMANTIC, "DUPLICATE_TABLE", key)
        self.tables[key] = replace(schema, name=key)

    def unregister_table(self, name: str) -> None:
        key = name.lower()
        if key == "__catalog" or (key in self.tables and self.tables[key].table_id == 0):
            raise MiniSQLError(ErrorStage.SEMANTIC, "PROTECTED_TABLE", key)
        if key not in self.tables:
            raise MiniSQLError(ErrorStage.SEMANTIC, "UNKNOWN_TABLE", key)
        del self.tables[key]


class MemoryStorage:
    """无编码、无缓存、无文件的记录接口替身；仅供执行器隔离测试。"""

    def __init__(self) -> None:
        self.tables: dict[int, TableSchema] = {}
        self.records: dict[int, dict[RecordId, Row]] = {}
        self.next_slot: dict[int, int] = {}
        self.next_table = 1

    def create_table(self, schema: TableSchema) -> TableSchema:
        table_id = self.next_table if schema.table_id is None else schema.table_id
        if table_id in self.tables:
            raise MiniSQLError(ErrorStage.STORAGE, "DUPLICATE_TABLE", str(table_id))
        if any(s.name.lower() == schema.name.lower() for s in self.tables.values()):
            raise MiniSQLError(ErrorStage.STORAGE, "DUPLICATE_TABLE", schema.name)
        assigned = replace(schema, table_id=table_id)
        self.tables[table_id] = assigned
        self.records[table_id] = {}
        self.next_slot[table_id] = 0
        self.next_table = max(self.next_table, table_id + 1)
        return assigned

    def _table_id(self, schema: TableSchema) -> int:
        if schema.table_id not in self.tables:
            raise MiniSQLError(ErrorStage.STORAGE, "UNKNOWN_TABLE", schema.name)
        assert schema.table_id is not None
        return schema.table_id

    def drop_table(self, schema: TableSchema) -> None:
        table_id = self._table_id(schema)
        if table_id == 0 or schema.name.lower() == "__catalog":
            raise MiniSQLError(ErrorStage.STORAGE, "PROTECTED_TABLE", schema.name)
        del self.tables[table_id]
        del self.records[table_id]
        del self.next_slot[table_id]

    def insert(self, schema: TableSchema, row: Row) -> RecordId:
        table_id = self._table_id(schema)
        if len(row) != len(schema.columns):
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "列数不匹配")
        record_id = RecordId(table_id, self.next_slot[table_id])
        self.next_slot[table_id] += 1
        self.records[table_id][record_id] = row
        return record_id

    def scan(self, schema: TableSchema) -> Iterator[StoredRecord]:
        table_id = self._table_id(schema)
        return iter(StoredRecord(rid, row) for rid, row in tuple(self.records[table_id].items()))

    def delete(self, schema: TableSchema, record_id: RecordId) -> None:
        table_id = self._table_id(schema)
        if record_id not in self.records[table_id]:
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", str(record_id))
        del self.records[table_id][record_id]

    def flush(self) -> None:
        pass  # 测试替身无持久化

    def close(self) -> None:
        pass  # 测试替身无文件资源
