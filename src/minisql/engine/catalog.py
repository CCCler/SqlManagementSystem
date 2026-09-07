from minisql.contracts.interfaces import RecordStorage
from minisql.contracts.models import ColumnSchema, DataType, TableSchema

# table_id=0 保留给系统表；列信息按表内顺序记录。
SYSTEM_CATALOG = TableSchema(
    "__catalog",
    (
        ColumnSchema("table_id", DataType.INT),
        ColumnSchema("table_name", DataType.VARCHAR),
        ColumnSchema("column_index", DataType.INT),
        ColumnSchema("column_name", DataType.VARCHAR),
        ColumnSchema("column_type", DataType.VARCHAR),
    ),
    table_id=0,
)


class PersistentCatalog:
    def __init__(self, storage: RecordStorage) -> None:
        self.storage = storage

    def bootstrap(self) -> None:
        raise NotImplementedError("成员三：直接通过存储接口初始化或恢复系统表")

    def get_table(self, name: str) -> TableSchema | None:
        raise NotImplementedError("成员三：查询表结构")

    def list_tables(self) -> tuple[TableSchema, ...]:
        raise NotImplementedError("成员三：列出用户表")

    def register_table(self, schema: TableSchema) -> None:
        raise NotImplementedError("成员三：注册已分配 table_id 的表结构")
