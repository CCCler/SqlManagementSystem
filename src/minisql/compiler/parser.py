from minisql.contracts.ast import (
    BinaryExpr, CreateTableStmt, DeleteStmt, DropTableStmt, ExplainStmt, Identifier, InsertStmt,
    Literal, OrderTerm, SelectStmt, Statement, TransactionStmt, UnaryExpr,
)
from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, SourcePosition, TableSchema, Token, TokenType


MAX_EXPRESSION_COMPLEXITY = 64


class Parser:
    def parse(self, tokens: tuple[Token, ...]) -> Statement:
        if not tokens or tokens[-1].type is not TokenType.EOF:
            raise MiniSQLError(ErrorStage.SYNTAX, "UNEXPECTED_TOKEN", "缺少 EOF",
                               tokens[-1].position if tokens else SourcePosition(1, 1), ("EOF",))
        return _Parser(tokens).statement()


class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.index = 0

    @property
    def current(self):
        return self.tokens[self.index]

    def matches(self, text):
        return self.current.type in (TokenType.KEYWORD, TokenType.OPERATOR, TokenType.DELIMITER) and self.current.lexeme.upper() == text

    def take(self):
        token = self.current
        if token.type is not TokenType.EOF:
            self.index += 1
        return token

    def error(self, *expected):
        raise MiniSQLError(ErrorStage.SYNTAX, "UNEXPECTED_TOKEN",
                           f"意外符号：{self.current.lexeme or 'EOF'}", self.current.position, expected)

    def expect(self, text):
        if not self.matches(text):
            self.error(text)
        return self.take()

    def identifier(self):
        if self.current.type is not TokenType.IDENTIFIER:
            self.error("IDENTIFIER")
        token = self.take()
        return Identifier(token.lexeme.lower(), token.position)

    def separated(self, reader):
        items = [reader()]
        while self.matches(","):
            self.take()
            items.append(reader())
        return tuple(items)

    def column(self):
        name = self.identifier()
        if not (self.matches("INT") or self.matches("VARCHAR")):
            self.error("INT", "VARCHAR")
        return ColumnSchema(name.name, DataType(self.take().lexeme.upper()))

    def statement(self):
        position = self.current.position
        if any(self.matches(word) for word in ("BEGIN", "COMMIT", "ROLLBACK")):
            result = TransactionStmt(self.take().lexeme.upper(), position)
        elif self.matches("CREATE"):
            self.take()
            self.expect("TABLE")
            table = self.identifier()
            self.expect("(")
            columns = self.separated(self.column)
            self.expect(")")
            result = CreateTableStmt(TableSchema(table.name, columns), position)
        elif self.matches("DROP"):
            self.take()
            self.expect("TABLE")
            result = DropTableStmt(self.identifier(), position)
        elif self.matches("INSERT"):
            self.take()
            self.expect("INTO")
            table = self.identifier()
            self.expect("(")
            columns = self.separated(self.identifier)
            self.expect(")")
            self.expect("VALUES")
            self.expect("(")
            values = self.separated(self.literal)
            self.expect(")")
            result = InsertStmt(table, columns, values, position)
        elif self.matches("SELECT"):
            result = self.select_body(position)
        elif self.matches("DELETE"):
            result = self.delete_body(position)
        elif self.matches("EXPLAIN"):
            self.take()
            result = ExplainStmt(self.explain_target(), position)
        else:
            self.error("CREATE", "INSERT", "SELECT", "DELETE", "DROP", "EXPLAIN", "BEGIN", "COMMIT", "ROLLBACK")
        self.expect(";")
        if self.current.type is not TokenType.EOF or self.index != len(self.tokens) - 1:
            self.error("EOF")
        return result

    def select_body(self, position):
        self.take()  # SELECT
        distinct = False
        if self.matches("DISTINCT"):
            self.take()
            distinct = True
        if self.matches("*"):
            self.take()
            columns = None
        else:
            columns = self.separated(self.identifier)
        self.expect("FROM")
        table = self.identifier()
        where = self.where()
        order_by = self.order_by_clause()
        limit, offset = self.limit_clause()
        return SelectStmt(
            table=table,
            columns=columns,
            where=where,
            position=position,
            distinct=distinct,
            limit=limit,
            order_by=order_by,
            offset=offset,
        )

    def delete_body(self, position):
        self.take()  # DELETE
        self.expect("FROM")
        table = self.identifier()
        return DeleteStmt(table, self.where(), position)

    def explain_target(self):
        if self.matches("SELECT"):
            return self.select_body(self.current.position)
        if self.matches("DELETE"):
            return self.delete_body(self.current.position)
        self.error("SELECT", "DELETE")

    def limit_clause(self):
        if not self.matches("LIMIT"):
            return None, None
        self.take()
        limit = self._non_negative_integer()
        offset = None
        if self.matches("OFFSET"):
            self.take()
            offset = self._non_negative_integer()
        return limit, offset

    def _non_negative_integer(self):
        token = self.current
        if token.type is TokenType.CONST and token.lexeme.isascii() and token.lexeme.isdigit():
            self.take()
            digits = token.lexeme.lstrip("0") or "0"
            return int(digits) if len(digits) <= 19 else 2 ** 63
        self.error("INTEGER")

    def order_by_clause(self):
        if not self.matches("ORDER"):
            return ()
        self.take()
        self.expect("BY")
        return self.separated(self.order_term)

    def order_term(self):
        column = self.identifier()
        descending = False
        if self.matches("ASC") or self.matches("DESC"):
            descending = self.take().lexeme.upper() == "DESC"
        return OrderTerm(column, descending)

    def where(self):
        if self.matches("WHERE"):
            self.take()
            # 在任何递归前预算结构量，同时约束括号、NOT 及左深二元表达式树。
            # 只计结构 Token，字符串内容和标识符长度不计入。
            complexity = 0
            for token in self.tokens[self.index:]:
                if (token.type is TokenType.OPERATOR or
                        token.type is TokenType.DELIMITER and token.lexeme == "(" or
                        token.type is TokenType.KEYWORD and token.lexeme.upper() in ("NOT", "AND", "OR")):
                    complexity += 1
                    if complexity > MAX_EXPRESSION_COMPLEXITY:
                        raise MiniSQLError(
                            ErrorStage.SYNTAX, "EXPRESSION_TOO_COMPLEX",
                            f"WHERE 表达式的左括号与运算符总数最多为 {MAX_EXPRESSION_COMPLEXITY}，请简化表达式",
                            token.position, ("SIMPLER_EXPRESSION",))
            return self.or_expr()
        return None

    def chain(self, reader, operators):
        left = reader()
        while any(self.matches(op) for op in operators):
            token = self.take()
            left = BinaryExpr(token.lexeme.upper(), left, reader(), token.position)
        return left

    def or_expr(self):
        return self.chain(self.and_expr, ("OR",))

    def and_expr(self):
        return self.chain(self.not_expr, ("AND",))

    def not_expr(self):
        if self.matches("NOT"):
            token = self.take()
            return UnaryExpr("NOT", self.not_expr(), token.position)
        left = self.additive()
        if any(self.matches(op) for op in ("=", "!=", "<>", "<", "<=", ">", ">=")):
            token = self.take()
            return BinaryExpr(token.lexeme, left, self.additive(), token.position)
        return left

    def additive(self):
        return self.chain(self.primary, ("+", "-"))

    def primary(self):
        if self.matches("("):
            self.take()
            result = self.or_expr()
            self.expect(")")
            return result
        if self.current.type is TokenType.IDENTIFIER:
            return self.identifier()
        return self.literal()

    def literal(self):
        position = self.current.position
        negative = self.matches("-")
        if negative:
            self.take()
        token = self.current
        if token.type is TokenType.CONST and token.lexeme.isascii() and token.lexeme.isdigit():
            self.take()
            # 避免超长数字触发 Python 的十进制转换长度限制。
            digits = token.lexeme.lstrip("0") or "0"
            value = int(digits) if len(digits) <= 19 else 2 ** 63 + 1
            return Literal(-value if negative else value, DataType.INT, position)
        if negative:
            self.error("INTEGER")
        if token.type is TokenType.CONST and token.lexeme.startswith("'"):
            self.take()
            return Literal(token.lexeme[1:-1].replace("''", "'"), DataType.VARCHAR, position)
        if self.matches("TRUE") or self.matches("FALSE"):
            self.take()
            return Literal(token.lexeme.upper() == "TRUE", DataType.BOOL, position)
        self.error("INTEGER", "STRING", "TRUE", "FALSE")
