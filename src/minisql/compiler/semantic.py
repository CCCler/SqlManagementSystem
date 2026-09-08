from dataclasses import replace
from minisql.contracts.ast import (
    CreateTableStmt, Identifier, InsertStmt, Literal, SelectStmt, Statement, UnaryExpr,
)
from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.interfaces import CatalogReader
from minisql.contracts.models import DataType
from minisql.contracts.plans import SemanticResult


class SemanticAnalyzer:
    def analyze(self, statement: Statement, catalog: CatalogReader) -> SemanticResult:
        def fail(code, reason, position=statement.position):
            raise MiniSQLError(ErrorStage.SEMANTIC, code, reason, position)

        def normalize_schema(schema):
            return replace(schema, name=schema.name.lower(), columns=tuple(
                replace(c, name=c.name.lower()) for c in schema.columns))

        def identifier(node):
            return replace(node, name=node.name.lower())

        if isinstance(statement, CreateTableStmt):
            schema = normalize_schema(statement.schema)
            if catalog.get_table(schema.name) is not None:
                fail("DUPLICATE_TABLE", f"表已存在：{schema.name}")
            seen = set()
            for column in schema.columns:
                if column.name in seen:
                    fail("DUPLICATE_COLUMN", f"重复列：{column.name}")
                seen.add(column.name)
                if column.data_type not in (DataType.INT, DataType.VARCHAR):
                    fail("TYPE_MISMATCH", "表列只支持 INT/VARCHAR")
            return SemanticResult(replace(statement, schema=schema), schema)

        table = identifier(statement.table)
        schema = catalog.get_table(table.name)
        if schema is None:
            fail("UNKNOWN_TABLE", f"未知表：{table.name}", table.position)
        schema = normalize_schema(schema)
        column_types = {c.name: c.data_type for c in schema.columns}

        def column(node):
            node = identifier(node)
            if node.name not in column_types:
                fail("UNKNOWN_COLUMN", f"未知列：{node.name}", node.position)
            return node

        def expression(node):
            if isinstance(node, Identifier):
                node = column(node)
                return node, column_types[node.name]
            if isinstance(node, Literal):
                types = {DataType.INT: int, DataType.VARCHAR: str, DataType.BOOL: bool}
                if type(node.value) is not types[node.data_type]:
                    fail("TYPE_MISMATCH", "常量类型不匹配", node.position)
                if node.data_type is DataType.INT and not -(2 ** 63) <= node.value < 2 ** 63:
                    fail("INTEGER_OUT_OF_RANGE", "整数超出有符号 64 位范围", node.position)
                return node, node.data_type
            if isinstance(node, UnaryExpr):
                operand, kind = expression(node.operand)
                if node.operator.upper() != "NOT" or kind is not DataType.BOOL:
                    fail("TYPE_MISMATCH", "NOT 需要 BOOL 操作数", node.position)
                return replace(node, operator="NOT", operand=operand), DataType.BOOL
            left, left_type = expression(node.left)
            right, right_type = expression(node.right)
            op = node.operator.upper()
            if op in ("+", "-"):
                valid = left_type is right_type is DataType.INT
                result_type = DataType.INT
            elif op in ("AND", "OR"):
                valid = left_type is right_type is DataType.BOOL
                result_type = DataType.BOOL
            else:
                valid = op in ("=", "!=", "<>", "<", "<=", ">", ">=") and left_type is right_type
                result_type = DataType.BOOL
            if not valid:
                fail("TYPE_MISMATCH", f"运算符 {op} 的操作数类型不匹配", node.position)
            return replace(node, operator=op, left=left, right=right), result_type

        if isinstance(statement, InsertStmt):
            columns = tuple(column(c) for c in statement.columns)
            seen = set()
            for c in columns:
                if c.name in seen:
                    fail("DUPLICATE_COLUMN", f"重复列：{c.name}", c.position)
                seen.add(c.name)
            if len(columns) != len(statement.values):
                fail("VALUE_COUNT_MISMATCH", "INSERT 列数和值数量不匹配")
            if seen != set(column_types):
                fail("MISSING_COLUMN", "INSERT 必须提供全部表列")
            for c, value in zip(columns, statement.values):
                _, kind = expression(value)
                if kind is not column_types[c.name]:
                    fail("TYPE_MISMATCH", f"列 {c.name} 的值类型不匹配", value.position)
            bound = replace(statement, table=table, columns=columns)
        else:
            where = statement.where
            if where is not None:
                where, kind = expression(where)
                if kind is not DataType.BOOL:
                    fail("TYPE_MISMATCH", "WHERE 必须为 BOOL", where.position)
            bound = replace(statement, table=table, where=where)
            if isinstance(bound, SelectStmt) and bound.columns is not None:
                bound = replace(bound, columns=tuple(column(c) for c in bound.columns))
        return SemanticResult(bound, schema)
