from minisql.contracts.ast import CreateTableStmt, DropTableStmt, InsertStmt, SelectStmt, TransactionStmt
from minisql.contracts.plans import CreateTable, Delete, DropTable, Filter, Insert, Plan, Project, SemanticResult, SeqScan, TransactionControl


class Planner:
    def build(self, semantic: SemanticResult) -> Plan:
        statement, schema = semantic.statement, semantic.schema
        if isinstance(statement, TransactionStmt):
            return TransactionControl(statement.action)
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
            return Project(tuple(c.name for c in columns), source)
        return Delete(schema, source)
