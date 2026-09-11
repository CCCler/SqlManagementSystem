"""PersistentCatalog：元数据通过 RecordStorage 持久化在系统表中。

bootstrap 直接通过存储接口初始化或恢复目录，不经过 SQL 编译；
register_table 注册已分配 table_id 的表结构。"""
from dataclasses import replace
import re

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


def _column_type_text(column: ColumnSchema) -> str:
    """复用目录的 VARCHAR 类型字段保存参数，不改变系统表布局。"""
    p, s = column.precision, column.scale
    if p is None and s is None:
        return column.data_type.value
    if (column.data_type is not DataType.DECIMAL or type(p) is not int or
            type(s) is not int or not 1 <= p <= 38 or not 0 <= s <= p):
        raise MiniSQLError(ErrorStage.STORAGE, 'INVALID_SCHEMA', '非法字段类型参数')
    return f'DECIMAL({p},{s})'


def _restore_column(name: str, text: str) -> ColumnSchema:
    # 旧库的 INT/VARCHAR/DECIMAL 等裸类型名继续原样恢复。
    match = re.fullmatch(r'DECIMAL\(([0-9]{1,2}),([0-9]{1,2})\)', text) if isinstance(text, str) else None
    try:
        if match:
            column = ColumnSchema(name, DataType.DECIMAL, int(match[1]), int(match[2]))
            _column_type_text(column)
            return column
        return ColumnSchema(name, DataType(text))
    except (ValueError, TypeError, MiniSQLError) as error:
        raise MiniSQLError(ErrorStage.STORAGE, 'CORRUPT_CATALOG', '目录中存在非法字段类型描述') from error


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
                _restore_column(column_name, column_type)
            )
        self.tables = {
            table_name.lower(): TableSchema(
                table_name, tuple(columns[index] for index in sorted(columns)), table_id=table_id,
            )
            for (table_id, table_name), columns in columns_by_table.items()
        }

    def get_table(self, name: str) -> TableSchema | None:
        """公开查找：不暴露 "__" 前缀的系统表（编译器与 SQL 不可见）。"""
        key = name.lower()
        if key.startswith("__"):
            return None
        return self.tables.get(key)

    def _resolve_internal(self, name: str) -> TableSchema | None:
        """内部查找：含对象系统表（PersistentObjectCatalog/账户存储使用）。"""
        return self.tables.get(name.lower())

    def list_tables(self) -> tuple[TableSchema, ...]:
        """只返回用户表，不暴露系统表。"""
        return tuple(
            schema for schema in self.tables.values()
            if schema.table_id != 0 and not schema.name.startswith("__")
        )

    def register_table(self, schema: TableSchema) -> None:
        """注册已分配 table_id 的表结构；重复表名报错。"""
        key = schema.name.lower()
        if key in self.tables:
            raise MiniSQLError(ErrorStage.SEMANTIC, "DUPLICATE_TABLE", key)
        if schema.table_id is None:
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_RECORD", "表尚未分配 table_id")
        type_texts = tuple(_column_type_text(column) for column in schema.columns)
        for index, column in enumerate(schema.columns):
            self.storage.insert(
                SYSTEM_CATALOG, (schema.table_id, key, index, column.name, type_texts[index]),
            )
        self.storage.flush()
        self.tables[key] = replace(schema, name=key)

    def unregister_table(self, name: str) -> None:
        key = name.lower()
        if key == "__catalog" or (key in self.tables and self.tables[key].table_id == 0):
            raise MiniSQLError(ErrorStage.SEMANTIC, "PROTECTED_TABLE", "不能删除系统目录表")
        schema = self.get_table(key)
        if schema is None:
            raise MiniSQLError(ErrorStage.SEMANTIC, "UNKNOWN_TABLE", name)
        records = [record for record in self.storage.scan(SYSTEM_CATALOG)
                   if record.row[0] == schema.table_id]
        for record in records:
            self.storage.delete(SYSTEM_CATALOG, record.record_id)
        self.storage.flush()
        del self.tables[key]
