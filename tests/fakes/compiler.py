"""测试替身：极简 SQL 编译器，仅覆盖 CREATE/INSERT/SELECT */DELETE。

用于 Database 与 CLI 的隔离测试；不代表成员一真实编译器的行为。"""
import re

from minisql.contracts.ast import (
    CreateTableStmt, DeleteStmt, Identifier, InsertStmt, Literal, SelectStmt,
)
from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.interfaces import CatalogReader
from minisql.contracts.models import ColumnSchema, DataType, SourcePosition, TableSchema
from minisql.contracts.plans import (
    CompilationResult, CreateTable, Delete, Insert, SeqScan, SemanticResult,
)

POS = SourcePosition(1, 1)

_CREATE = re.compile(r"CREATE\s+TABLE\s+(\w+)\s*\((.*)\)\s*;?\s*$", re.IGNORECASE | re.DOTALL)
_INSERT = re.compile(
    r"INSERT\s+INTO\s+(\w+)\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_SELECT = re.compile(r"SELECT\s+\*\s+FROM\s+(\w+)\s*;?\s*$", re.IGNORECASE | re.DOTALL)
_DELETE = re.compile(r"DELETE\s+FROM\s+(\w+)\s*;?\s*$", re.IGNORECASE | re.DOTALL)


def _parse_value(text: str) -> int | str | bool:
    text = text.strip()
    if text.startswith("'"):
        return text[1:-1].replace("''", "'")
    if text.upper() == "TRUE":
        return True
    if text.upper() == "FALSE":
        return False
    return int(text)


def _value_type(value: int | str | bool) -> DataType:
    if isinstance(value, bool):
        return DataType.BOOL
    if isinstance(value, str):
        return DataType.VARCHAR
    return DataType.INT


def _resolve(catalog: CatalogReader, name: str) -> TableSchema:
    schema = catalog.get_table(name)
    if schema is None:
        raise MiniSQLError(ErrorStage.SEMANTIC, "UNKNOWN_TABLE", name)
    return schema


class FakeCompiler:
    def split_statements(self, sql: str) -> tuple[str, ...]:
        return tuple(part.strip() for part in sql.split(";") if part.strip())

    def compile(self, sql: str, catalog: CatalogReader) -> CompilationResult:
        create = _CREATE.match(sql)
        if create:
            name = create.group(1).lower()
            columns = tuple(
                ColumnSchema(parts[0], DataType(parts[1].upper()))
                for parts in (item.split() for item in create.group(2).split(","))
            )
            schema = TableSchema(name, columns)
            statement = CreateTableStmt(schema, POS)
            return CompilationResult(
                (), statement, SemanticResult(statement, schema),
                CreateTable(schema), CreateTable(schema),
            )
        insert = _INSERT.match(sql)
        if insert:
            schema = _resolve(catalog, insert.group(1))
            names = [name.strip().lower() for name in insert.group(2).split(",")]
            values = [_parse_value(value) for value in insert.group(3).split(",")]
            by_name = dict(zip(names, values))
            if set(by_name) != {column.name for column in schema.columns}:
                raise MiniSQLError(ErrorStage.SEMANTIC, "UNKNOWN_COLUMN", "INSERT 必须提供全部表列")
            row = tuple(by_name[column.name] for column in schema.columns)
            statement = InsertStmt(
                Identifier(schema.name, POS),
                tuple(Identifier(name, POS) for name in names),
                tuple(Literal(value, _value_type(value), POS) for value in values),
                POS,
            )
            return CompilationResult(
                (), statement, SemanticResult(statement, schema),
                Insert(schema, row), Insert(schema, row),
            )
        select = _SELECT.match(sql)
        if select:
            schema = _resolve(catalog, select.group(1))
            statement = SelectStmt(Identifier(schema.name, POS), None, None, POS)
            return CompilationResult(
                (), statement, SemanticResult(statement, schema),
                SeqScan(schema), SeqScan(schema),
            )
        delete = _DELETE.match(sql)
        if delete:
            schema = _resolve(catalog, delete.group(1))
            statement = DeleteStmt(Identifier(schema.name, POS), None, POS)
            return CompilationResult(
                (), statement, SemanticResult(statement, schema),
                Delete(schema, SeqScan(schema)), Delete(schema, SeqScan(schema)),
            )
        raise MiniSQLError(ErrorStage.SYNTAX, "UNEXPECTED_TOKEN", "替身编译器不支持该语句")
