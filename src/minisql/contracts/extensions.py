"""成员一扩展契约；仅描述编译结果，不执行或保存数据。"""
from dataclasses import dataclass, field
from typing import Protocol
from minisql.contracts.models import ColumnSchema, SourcePosition, TableSchema


@dataclass(frozen=True)
class TypeSpec:
    kind: str
    precision: int | None = None
    scale: int | None = None
    nullable: bool = True


@dataclass(frozen=True)
class FieldBinding:
    scope: int
    source: int
    ordinal: int
    qualifier: str
    name: str
    type: TypeSpec


@dataclass(frozen=True)
class OutputField:
    name: str
    type: TypeSpec


@dataclass(frozen=True)
class Expr:
    op: str
    args: tuple[object, ...] = ()
    position: SourcePosition = SourcePosition(1, 1)
    type: TypeSpec | None = None
    binding: FieldBinding | None = None


@dataclass(frozen=True)
class SelectItem:
    expression: Expr
    alias: str | None = None


@dataclass(frozen=True)
class Relation:
    name: str | None = None
    alias: str | None = None
    query: object = None
    position: SourcePosition = SourcePosition(1, 1)


@dataclass(frozen=True)
class JoinSource:
    kind: str
    left: object
    right: object
    on: Expr | None = None


@dataclass(frozen=True)
class Query:
    items: tuple[SelectItem, ...] = ()
    source: Relation | JoinSource | None = None
    where: Expr | None = None
    group_by: tuple[Expr, ...] = ()
    having: Expr | None = None
    distinct: bool = False
    order_by: tuple[tuple[Expr, bool], ...] = ()
    limit: int | None = None
    offset: int | None = None
    set_op: str | None = None
    left: object = None
    right: object = None
    position: SourcePosition = SourcePosition(1, 1)


@dataclass(frozen=True)
class Constraint:
    kind: str
    columns: tuple[str, ...] = ()
    name: str | None = None
    reference_table: str | None = None
    reference_columns: tuple[str, ...] = ()
    expression: Expr | None = None


@dataclass(frozen=True)
class Command:
    kind: str
    name: str
    payload: tuple[object, ...] = ()
    position: SourcePosition = SourcePosition(1, 1)
    password: str | None = field(default=None, repr=False, metadata={"sensitive": True})


@dataclass(frozen=True)
class ObjectDependency:
    kind: str
    name: str


@dataclass(frozen=True)
class ExtendedPlan:
    operator: str
    children: tuple[object, ...] = ()
    expressions: tuple[Expr, ...] = ()
    output: tuple[OutputField, ...] = ()
    attributes: tuple[tuple[str, object], ...] = ()
    capabilities: tuple[str, ...] = ()
    dependencies: tuple[ObjectDependency, ...] = ()
    password: str | None = field(default=None, repr=False, metadata={"sensitive": True})


@dataclass(frozen=True)
class BoundStatement:
    ast: Query | Command
    plan: ExtendedPlan


@dataclass(frozen=True)
class ViewDefinition:
    name: str
    query: Query | str
    columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class IndexDefinition:
    name: str
    table: str
    columns: tuple[str, ...]
    unique: bool = False
    available: bool = True


@dataclass(frozen=True)
class TriggerDefinition:
    name: str
    table: str
    event: str
    writes: tuple[tuple[str, str], ...] = ()


class ExtendedCatalogReader(Protocol):
    def get_table(self, name: str) -> TableSchema | None: ...
    def list_tables(self) -> tuple[TableSchema, ...]: ...
    def get_view(self, name: str) -> ViewDefinition | None: ...
    def get_index(self, name: str) -> IndexDefinition | None: ...
    def list_indexes(self, table: str) -> tuple[IndexDefinition, ...]: ...
    def get_trigger(self, name: str) -> TriggerDefinition | None: ...
    def list_triggers(self) -> tuple[TriggerDefinition, ...]: ...
    def has_database(self, name: str) -> bool: ...
    def get_account(self, name: str) -> object | None: ...
    def get_dependencies(self, kind: str, name: str) -> tuple[ObjectDependency, ...]: ...


def column_type(column: ColumnSchema) -> TypeSpec:
    precision, scale = column.precision, column.scale
    if column.data_type.value == 'DECIMAL':
        precision = 18 if precision is None else precision
        scale = 2 if scale is None else scale
    return TypeSpec(column.data_type.value, precision, scale, column.nullable)
