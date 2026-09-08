from minisql.contracts.ast import (
    BinaryExpr, CreateTableStmt, DeleteStmt, Identifier, InsertStmt,
    Literal, SelectStmt, Statement, UnaryExpr,
)
from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, SourcePosition, TableSchema, Token, TokenType


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
        if self.matches("CREATE"):
            self.take()
            self.expect("TABLE")
            table = self.identifier()
            self.expect("(")
            columns = self.separated(self.column)
            self.expect(")")
            result = CreateTableStmt(TableSchema(table.name, columns), position)
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
            self.take()
            if self.matches("*"):
                self.take()
                columns = None
            else:
                columns = self.separated(self.identifier)
            self.expect("FROM")
            table = self.identifier()
            result = SelectStmt(table, columns, self.where(), position)
        elif self.matches("DELETE"):
            self.take()
            self.expect("FROM")
            table = self.identifier()
            result = DeleteStmt(table, self.where(), position)
        else:
            self.error("CREATE", "INSERT", "SELECT", "DELETE")
        self.expect(";")
        if self.current.type is not TokenType.EOF or self.index != len(self.tokens) - 1:
            self.error("EOF")
        return result

    def where(self):
        if self.matches("WHERE"):
            self.take()
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
