from dataclasses import dataclass
from typing import TypeAlias
from minisql.contracts.ast import Expression, Statement
from minisql.contracts.models import Row, TableSchema, Token


@dataclass(frozen=True)
class CreateTable:
    schema: TableSchema


@dataclass(frozen=True)
class Insert:
    schema: TableSchema
    values: Row  # 转换为表定义的列顺序


@dataclass(frozen=True)
class SeqScan:
    schema: TableSchema


@dataclass(frozen=True)
class Filter:
    predicate: Expression
    source: "QueryPlan"


@dataclass(frozen=True)
class Project:
    columns: tuple[str, ...]
    source: "QueryPlan"
    distinct: bool = False
    limit: int | None = None


QueryPlan: TypeAlias = SeqScan | Filter | Project


@dataclass(frozen=True)
class Delete:
    schema: TableSchema
    source: SeqScan | Filter  # 删除路径必须保留 RecordId，禁止 Project


@dataclass(frozen=True)
class DropTable:
    schema: TableSchema


@dataclass(frozen=True)
class TransactionControl:
    action: str


@dataclass(frozen=True)
class Explain:
    plan: "Plan"


Plan: TypeAlias = CreateTable | Insert | QueryPlan | Delete | DropTable | TransactionControl | Explain


@dataclass(frozen=True)
class SemanticResult:
    statement: Statement
    schema: TableSchema | None  # 事务控制语句不绑定表。
    message: str = "语义检查通过"


@dataclass(frozen=True)
class CompilationResult:
    tokens: tuple[Token, ...]
    ast: Statement
    semantic: SemanticResult
    plan: Plan
    optimized_plan: Plan
