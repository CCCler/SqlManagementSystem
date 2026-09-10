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
class OrderTerm:
    column: Identifier
    descending: bool = False


@dataclass(frozen=True)
class SelectStmt:
    table: Identifier
    columns: tuple[Identifier, ...] | None  # None 表示 *
    where: Expression | None
    position: SourcePosition
    distinct: bool = False
    limit: int | None = None
    order_by: tuple[OrderTerm, ...] = ()
    offset: int | None = None


@dataclass(frozen=True)
class DeleteStmt:
    table: Identifier
    where: Expression | None
    position: SourcePosition


@dataclass(frozen=True)
class Assignment:
    column: Identifier
    value: Expression


@dataclass(frozen=True)
class UpdateStmt:
    table: Identifier
    assignments: tuple[Assignment, ...]
    where: Expression | None
    position: SourcePosition


@dataclass(frozen=True)
class DropTableStmt:
    table: Identifier
    position: SourcePosition


@dataclass(frozen=True)
class TransactionStmt:
    action: str
    position: SourcePosition


@dataclass(frozen=True)
class ExplainStmt:
    statement: "Statement"
    position: SourcePosition


Statement: TypeAlias = CreateTableStmt | InsertStmt | SelectStmt | DeleteStmt | UpdateStmt | DropTableStmt | TransactionStmt | ExplainStmt
