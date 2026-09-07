from dataclasses import dataclass
from typing import TypeAlias
from minisql.contracts.models import DataType, SourcePosition, TableSchema, Value


@dataclass(frozen=True)
class Literal:
    value: Value
    data_type: DataType
    position: SourcePosition


@dataclass(frozen=True)
class Identifier:
    name: str
    position: SourcePosition


@dataclass(frozen=True)
class UnaryExpr:
    operator: str
    operand: "Expression"
    position: SourcePosition


@dataclass(frozen=True)
class BinaryExpr:
    operator: str
    left: "Expression"
    right: "Expression"
    position: SourcePosition


Expression: TypeAlias = Literal | Identifier | UnaryExpr | BinaryExpr


@dataclass(frozen=True)
class CreateTableStmt:
    schema: TableSchema
    position: SourcePosition


@dataclass(frozen=True)
class InsertStmt:
    table: Identifier
    columns: tuple[Identifier, ...]
    values: tuple[Literal, ...]
    position: SourcePosition


@dataclass(frozen=True)
class SelectStmt:
    table: Identifier
    columns: tuple[Identifier, ...] | None  # None 表示 *
    where: Expression | None
    position: SourcePosition


@dataclass(frozen=True)
class DeleteStmt:
    table: Identifier
    where: Expression | None
    position: SourcePosition


Statement: TypeAlias = CreateTableStmt | InsertStmt | SelectStmt | DeleteStmt
