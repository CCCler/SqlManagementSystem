"""PersistentCatalog：元数据通过 RecordStorage 持久化在系统表中。

bootstrap 直接通过存储接口初始化或恢复目录，不经过 SQL 编译；
register_table 注册已分配 table_id 的表结构。"""
from dataclasses import replace

from minisql.contracts.errors import ErrorStage, MiniSQLError
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
        self.tables: dict[str, TableSchema] = {}

    def bootstrap(self) -> None:
        """初始化新库的系统表，或从已有系统表恢复目录。"""
        try:
            self.storage.create_table(SYSTEM_CATALOG)
        except MiniSQLError as error:
            if error.code != "DUPLICATE_TABLE":
                raise
        columns_by_table: dict[tuple[int, str], dict[int, ColumnSchema]] = {}
        for record in self.storage.scan(SYSTEM_CATALOG):
            table_id, table_name, column_index, column_name, column_type = record.row
            if table_id == 0:
                continue
            columns_by_table.setdefault((table_id, table_name), {})[column_index] = (
                ColumnSchema(column_name, DataType(column_type))
            )
        self.tables = {
            table_name.lower(): TableSchema(
                table_name, tuple(columns[index] for index in sorted(columns)), table_id=table_id,
            )
            for (table_id, table_name), columns in columns_by_table.items()
        }

    def get_table(self, name: str) -> TableSchema | None:
        return self.tables.get(name.lower())

    def list_tables(self) -> tuple[TableSchema, ...]:
        """只返回用户表，不暴露系统表。"""
        return tuple(schema for schema in self.tables.values() if schema.table_id != 0)

    def register_table(self, schema: TableSchema) -> None:
        """注册已分配 table_id 的表结构；重复表名报错。"""
        key = schema.name.lower()
        if key in self.tables:
            raise MiniSQLError(ErrorStage.SEMANTIC, "DUPLICATE_TABLE", key)
        if schema.table_id is None:
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "表尚未分配 table_id")
        for index, column in enumerate(schema.columns):
            self.storage.insert(
                SYSTEM_CATALOG, (schema.table_id, key, index, column.name, column.data_type.value),
            )
        self.storage.flush()
        self.tables[key] = replace(schema, name=key)
