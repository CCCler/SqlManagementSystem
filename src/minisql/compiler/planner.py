from minisql.contracts.ast import UpdateStmt, CreateTableStmt, DropTableStmt, ExplainStmt, InsertStmt, SelectStmt, TransactionStmt
from minisql.contracts.plans import Update, CreateTable, Delete, DropTable, Explain, Filter, Insert, Plan, Project, SemanticResult, SeqScan, TransactionControl


class Planner:
    def build(self, semantic: SemanticResult) -> Plan:
        statement, schema = semantic.statement, semantic.schema
        from minisql.contracts.extensions import BoundStatement
        if isinstance(statement, BoundStatement):
            return statement.plan
        if isinstance(statement, TransactionStmt):
            return TransactionControl(statement.action)
        if isinstance(statement, ExplainStmt):
            return Explain(self.build(SemanticResult(statement.statement, schema)))
        if isinstance(statement, CreateTableStmt):
            return CreateTable(schema)
        if isinstance(statement, DropTableStmt):
            return DropTable(schema)
        if isinstance(statement, InsertStmt):
            values = {c.name: v.value for c, v in zip(statement.columns, statement.values)}
            return Insert(schema, tuple(values[c.name] for c in schema.columns))
        source = SeqScan(schema)
        if statement.where is not None:
            source = Filter(statement.where, source)
        if isinstance(statement, SelectStmt):
            columns = schema.columns if statement.columns is None else statement.columns
            order_by = tuple((term.column.name, term.descending) for term in statement.order_by)
            return Project(
                columns=tuple(c.name for c in columns),
                source=source,
                distinct=statement.distinct,
                limit=statement.limit,
                order_by=order_by,
                offset=statement.offset,
            )
        if isinstance(statement, UpdateStmt):
            return Update(schema, tuple((a.column.name, a.value) for a in statement.assignments), source)
        return Delete(schema, source)
