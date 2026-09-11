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
    order_by: tuple[tuple[str, bool], ...] = ()  # (列名, 是否降序)
    offset: int | None = None


@dataclass(frozen=True)
class EmptyScan:
    """恒假条件的空结果计划：不扫描存储，仅携带输出列名。"""

    columns: tuple[str, ...]


QueryPlan: TypeAlias = SeqScan | Filter | Project | EmptyScan


@dataclass(frozen=True)
class Delete:
    schema: TableSchema
    source: SeqScan | Filter | EmptyScan  # 删除路径必须保留 RecordId，禁止 Project


@dataclass(frozen=True)
class Update:
    schema: TableSchema
    assignments: tuple[tuple[str, Expression], ...]
    source: SeqScan | Filter | EmptyScan


@dataclass(frozen=True)
class DropTable:
    schema: TableSchema


@dataclass(frozen=True)
class TransactionControl:
    action: str


@dataclass(frozen=True)
class Explain:
    plan: "Plan"


from minisql.contracts.extensions import ExtendedPlan, BoundStatement, OutputField, ObjectDependency

Plan: TypeAlias = ExtendedPlan | CreateTable | Insert | QueryPlan | Delete | Update | DropTable | TransactionControl | Explain


@dataclass(frozen=True)
class SemanticResult:
    statement: Statement | BoundStatement
    schema: TableSchema | None  # 事务控制语句不绑定表。
    message: str = "语义检查通过"


@dataclass(frozen=True)
class CompilationResult:
    tokens: tuple[Token, ...]
    ast: Statement
    semantic: SemanticResult
    plan: Plan
    optimized_plan: Plan
    output_fields: tuple[OutputField, ...] = ()
    dependencies: tuple[ObjectDependency, ...] = ()
    required_capabilities: tuple[str, ...] = ()
