"""扩展 SQL 递归下降解析；语义绑定和执行能力独立处理。"""
from dataclasses import replace
from decimal import Decimal
from datetime import date, time, datetime
import re
from minisql.contracts.extensions import (
    Command, Constraint, Expr, JoinSource, Query, Relation, SelectItem, TypeSpec,
)
from minisql.contracts.models import ColumnSchema, DataType, TableSchema, TokenType
from minisql.contracts.errors import ErrorStage, MiniSQLError

AGGREGATES = {"COUNT", "SUM", "AVG", "MAX", "MIN"}


class ExtendedParser:
    def __init__(self, tokens):
        self.tokens, self.i = tokens, 0
        self.depth = self.nodes = 0
        self.expression_budgets = []
        self.primary_depth = 0

    @property
    def token(self):
        return self.tokens[self.i]

    def at(self, word):
        return self.token.type not in (TokenType.CONST, TokenType.IDENTIFIER) and self.token.lexeme.upper() == word

    def take(self):
        t = self.token
        if t.type is not TokenType.EOF:
            self.i += 1
        if self.expression_budgets and (t.type is TokenType.OPERATOR or t.lexeme == "(" or t.lexeme.upper() in {"NOT", "AND", "OR", "IS", "IN", "LIKE", "BETWEEN", "EXISTS"}):
            self.expression_budgets[-1] += 1
            if self.expression_budgets[-1] > 64:
                self.fail("EXPRESSION_TOO_COMPLEX", "表达式结构超过 64", t.position)
        return t

    def accept(self, word):
        if self.at(word):
            return self.take()
        return None

    def expect(self, word):
        if not self.at(word):
            self.fail("UNEXPECTED_TOKEN", f"期望 {word}，实际 {self.token.lexeme or 'EOF'}", expected=(word,))
        return self.take()

    def fail(self, code, reason, position=None, expected=("VALID_TOKEN",)):
        raise MiniSQLError(ErrorStage.SYNTAX, code, reason, position or self.token.position, expected)

    def node(self, obj):
        self.nodes += 1
        if self.nodes > 4096:
            self.fail("STATEMENT_TOO_COMPLEX", "语句节点超过 4096")
        return obj

    def name(self):
        if self.token.type is not TokenType.IDENTIFIER:
            self.fail("UNEXPECTED_TOKEN", "期望标识符")
        return self.take().lexeme.lower()

    def names(self):
        self.expect("(")
        result = self.separated(self.name)
        self.expect(")")
        return result

    def separated(self, reader):
        result = [reader()]
        while self.accept(","):
            result.append(reader())
        return tuple(result)

    def integer(self):
        t = self.token
        if t.type is not TokenType.CONST or not t.lexeme.isascii() or not t.lexeme.isdigit():
            self.fail("UNEXPECTED_TOKEN", "期望非负整数")
        self.take()
        digits = t.lexeme.lstrip("0") or "0"
        return int(digits) if len(digits) <= 19 else 2**63

    def parse(self):
        result = self.statement()
        self.expect(";")
        if self.token.type is not TokenType.EOF:
            self.fail("UNEXPECTED_TOKEN", "语句结束后存在额外内容")
        return result

    def statement(self):
        pos = self.token.position
        if self.at("SELECT") or self.at("("):
            return self.query()
        if self.accept("EXPLAIN"):
            if self.at("EXPLAIN"):
                self.fail("UNEXPECTED_TOKEN", "EXPLAIN 不允许嵌套")
            inner = self.statement()
            return self.node(Command("EXPLAIN", "", (inner,), pos))
        if self.at("INSERT") or self.at("UPDATE") or self.at("DELETE"):
            kind = self.take().lexeme.upper()
            if kind == "INSERT":
                self.expect("INTO")
                name = self.name()
                columns = self.names()
                self.expect("VALUES")
                self.expect("(")
                values = self.separated(self.expression)
                self.expect(")")
                if any(v.op != "literal" and not (v.op == "column" and v.args[0] in ("old", "new")) for v in values):
                    self.fail("UNEXPECTED_TOKEN", "INSERT 值要求常量或触发器 OLD/NEW 字段")
                return self.node(Command(kind, name, (columns, values), pos))
            if kind == "DELETE":
                self.expect("FROM")
            name = self.name()
            assignments = ()
            if kind == "UPDATE":
                self.expect("SET")
                assignments = self.separated(self.assignment)
            where = self.expression() if self.accept("WHERE") else None
            return self.node(Command(kind, name, (assignments, where), pos))
        if self.accept("CREATE"):
            unique = bool(self.accept("UNIQUE"))
            if self.accept("INDEX"):
                name = self.name()
                self.expect("ON")
                table = self.name()
                return self.node(Command("CREATE INDEX", name, (table, self.names(), unique), pos))
            if unique:
                self.fail("UNEXPECTED_TOKEN", "UNIQUE 仅用于 CREATE INDEX")
            if self.accept("TABLE"):
                name = self.name()
                self.expect("(")
                columns, constraints = [], []
                while True:
                    if self.at_constraint():
                        constraints.append(self.constraint())
                    else:
                        col, cs = self.column()
                        columns.append(col)
                        constraints.extend(cs)
                    if not self.accept(","):
                        break
                self.expect(")")
                schema = TableSchema(name, tuple(columns), constraints=tuple(constraints))
                return self.node(Command("CREATE TABLE", name, (schema,), pos))
            if self.accept("VIEW"):
                name = self.name()
                columns = self.names() if self.at("(") else ()
                self.expect("AS")
                return self.node(Command("CREATE VIEW", name, (columns, self.query()), pos))
            if self.accept("TRIGGER"):
                name = self.name()
                self.expect("AFTER")
                event = self.take().lexeme.upper()
                if event not in ("INSERT", "UPDATE", "DELETE"):
                    self.fail("UNEXPECTED_TOKEN", "触发事件必须为 INSERT/UPDATE/DELETE")
                self.expect("ON")
                table = self.name()
                for word in ("FOR", "EACH", "ROW"):
                    self.expect(word)
                actions = []
                if self.accept("BEGIN"):
                    while not self.at("END"):
                        actions.append(self.trigger_action())
                        self.expect(";")
                    self.expect("END")
                    if not actions:
                        self.fail("UNEXPECTED_TOKEN", "触发器动作不能为空")
                else:
                    actions.append(self.trigger_action())
                return self.node(Command("CREATE TRIGGER", name, (table, event, tuple(actions)), pos))
            self.expect("USER")
            return self.user_command("CREATE USER", pos)
        if self.accept("DROP"):
            kind = self.take().lexeme.upper()
            if kind not in ("TABLE", "VIEW", "INDEX", "TRIGGER", "USER"):
                self.fail("UNEXPECTED_TOKEN", "不支持的 DROP 对象")
            return self.node(Command("DROP " + kind, self.name(), (), pos))
        if self.accept("ALTER"):
            if self.accept("USER"):
                return self.user_command("ALTER USER", pos)
            self.expect("TABLE")
            name = self.name()
            if self.accept("ADD"):
                if self.at_constraint():
                    payload = ("ADD CONSTRAINT", self.constraint())
                else:
                    self.expect("COLUMN")
                    payload = ("ADD COLUMN", *self.column())
            elif self.accept("DROP"):
                if self.accept("COLUMN"):
                    payload = ("DROP COLUMN", self.name())
                else:
                    self.expect("CONSTRAINT")
                    payload = ("DROP CONSTRAINT", self.name())
            elif self.accept("RENAME"):
                if self.accept("COLUMN"):
                    old = self.name()
                    self.expect("TO")
                    payload = ("RENAME COLUMN", old, self.name())
                else:
                    self.expect("TO")
                    payload = ("RENAME TABLE", self.name())
            else:
                self.expect("ALTER")
                self.expect("COLUMN")
                column = self.name()
                self.expect("TYPE")
                payload = ("ALTER TYPE", column, self.type_spec())
            return self.node(Command("ALTER TABLE", name, payload, pos))
        if self.at("GRANT") or self.at("REVOKE"):
            kind = self.take().lexeme.upper()
            def permission():
                word = self.take().lexeme.upper()
                if word == "CREATE":
                    word += " " + self.take().lexeme.upper()
                return word
            permissions = self.separated(permission)
            self.expect("ON")
            obj = self.take().lexeme.upper()
            if obj not in ("TABLE", "VIEW", "DATABASE", "INDEX", "TRIGGER"):
                self.fail("UNEXPECTED_TOKEN", "授权必须指定对象类型")
            name = self.name()
            self.expect("TO" if kind == "GRANT" else "FROM")
            return self.node(Command(kind, name, (permissions, obj.lower(), self.name()), pos))
        self.fail("UNEXPECTED_TOKEN", "不支持的语句")

    def user_command(self, kind, pos):
        name = self.name()
        self.expect("IDENTIFIED")
        self.expect("BY")
        token = self.token
        if token.type is not TokenType.CONST or not token.lexeme.startswith("'"):
            self.fail("UNEXPECTED_TOKEN", "密码必须为字符串")
        self.take()
        value = token.secret or token.lexeme
        return self.node(Command(kind, name, (), pos, value[1:-1].replace("''", "'")))

    def trigger_action(self):
        if not any(self.at(x) for x in ("INSERT", "UPDATE", "DELETE", "SELECT")):
            self.fail("UNEXPECTED_TOKEN", "触发器仅允许数据操作")
        return self.statement()

    def assignment(self):
        name = self.name()
        self.expect("=")
        return name, self.expression()

    def type_spec(self):
        t = self.take()
        kind = t.lexeme.upper()
        if kind not in {x.value for x in DataType}:
            self.fail("UNEXPECTED_TOKEN", "未知字段类型", t.position)
        p = s = None
        if kind == "DECIMAL":
            p, s = 18, 2
            if self.accept("("):
                p = self.integer()
                self.expect(",")
                s = self.integer()
                self.expect(")")
            if not 1 <= p <= 38 or not 0 <= s <= p:
                self.fail("INVALID_TYPE", "DECIMAL 要求 1≤p≤38 且 0≤s≤p", t.position)
        return TypeSpec(kind, p, s)

    def at_constraint(self):
        return any(self.at(w) for w in ("CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK"))

    def column(self):
        name = self.name()
        typ = self.type_spec()
        nullable, default, has_default = True, None, False
        constraints = []
        seen = set()
        while True:
            if self.at("NOT") or self.at("NULL"):
                if "NULL" in seen:
                    self.fail("DUPLICATE_CONSTRAINT", "重复可空性定义")
                seen.add("NULL")
                nullable = not bool(self.accept("NOT"))
                self.expect("NULL")
            elif self.accept("DEFAULT"):
                if has_default:
                    self.fail("DUPLICATE_CONSTRAINT", "重复 DEFAULT")
                default, has_default = self.expression(), True
            elif self.at_constraint() or self.at("REFERENCES"):
                c = self.constraint(name)
                constraints.append(c)
                if c.kind == "PRIMARY KEY":
                    nullable = False
            else:
                break
        col = ColumnSchema(name, DataType(typ.kind), typ.precision, typ.scale, nullable, default, has_default)
        return self.node(col), tuple(constraints)

    def constraint(self, column=None):
        name = self.name() if self.accept("CONSTRAINT") else None
        cols = (column,) if column else ()
        if self.accept("PRIMARY"):
            self.expect("KEY")
            return self.node(Constraint("PRIMARY KEY", cols or self.names(), name))
        if self.accept("UNIQUE"):
            return self.node(Constraint("UNIQUE", cols or self.names(), name))
        if self.accept("CHECK"):
            self.expect("(")
            expr = self.expression()
            self.expect(")")
            return self.node(Constraint("CHECK", (), name, expression=expr))
        if not column:
            self.expect("FOREIGN")
            self.expect("KEY")
            cols = self.names()
        self.expect("REFERENCES")
        table = self.name()
        ref = self.names()
        seen = set()
        while self.accept("ON"):
            event = self.take().lexeme.upper()
            if event not in ("UPDATE", "DELETE") or event in seen:
                self.fail("UNEXPECTED_TOKEN", "非法外键动作")
            seen.add(event)
            if not self.accept("RESTRICT"):
                self.expect("NO")
                self.expect("ACTION")
        return self.node(Constraint("FOREIGN KEY", cols, name, table, ref))

    def query(self):
        self.depth += 1
        if self.depth > 16:
            self.fail("QUERY_TOO_DEEP", "查询嵌套超过 16")
        try:
            result = self.union_query()
            orders = ()
            if self.accept("ORDER"):
                self.expect("BY")
                def order():
                    expr = self.expression()
                    desc = bool(self.accept("DESC"))
                    if not desc:
                        self.accept("ASC")
                    return expr, desc
                orders = self.separated(order)
            limit = offset = None
            if self.accept("LIMIT"):
                limit = self.integer()
                if self.accept("OFFSET"):
                    offset = self.integer()
            if orders or limit is not None:
                result = replace(result, order_by=orders, limit=limit, offset=offset)
            return result
        finally:
            self.depth -= 1

    def union_query(self):
        left = self.intersect_query()
        while self.at("UNION") or self.at("EXCEPT"):
            token = self.take()
            op = token.lexeme.upper()
            if op == "UNION" and self.accept("ALL"):
                op = "UNION ALL"
            left = self.node(Query(set_op=op, left=left, right=self.intersect_query(), position=token.position))
        return left

    def intersect_query(self):
        left = self.query_primary()
        while self.accept("INTERSECT"):
            left = self.node(Query(set_op="INTERSECT", left=left, right=self.query_primary(), position=left.position))
        return left

    def query_primary(self):
        if self.accept("("):
            query = self.query()
            self.expect(")")
            return query
        pos = self.expect("SELECT").position
        distinct = bool(self.accept("DISTINCT"))
        def item():
            if self.accept("*"):
                expr = self.node(Expr("star", position=pos))
            else:
                expr = self.expression()
            alias = self.alias()
            return self.node(SelectItem(expr, alias))
        items = self.separated(item)
        self.expect("FROM")
        source = self.relation()
        while self.at("JOIN") or any(self.at(x) for x in ("INNER", "LEFT", "RIGHT", "CROSS")):
            kind = "INNER" if self.at("JOIN") else self.take().lexeme.upper()
            self.expect("JOIN")
            right = self.relation()
            on = None
            if kind != "CROSS":
                self.expect("ON")
                on = self.expression()
            source = self.node(JoinSource(kind, source, right, on))
        where = self.expression() if self.accept("WHERE") else None
        group = ()
        if self.accept("GROUP"):
            self.expect("BY")
            group = self.separated(self.expression)
        having = self.expression() if self.accept("HAVING") else None
        return self.node(Query(items, source, where, group, having, distinct, position=pos))

    def alias(self):
        if self.accept("AS"):
            return self.name()
        if self.token.type is TokenType.IDENTIFIER:
            return self.name()
        return None

    def relation(self):
        pos = self.token.position
        if self.accept("("):
            q = self.query()
            self.expect(")")
            alias = self.alias()
            if alias is None:
                self.fail("UNEXPECTED_TOKEN", "FROM 子查询必须有别名")
            return self.node(Relation(alias=alias, query=q, position=pos))
        name = self.name()
        return self.node(Relation(name, self.alias(), position=pos))

    def expression(self):
        self.expression_budgets.append(0)
        try:
            return self.binary(0)
        finally:
            self.expression_budgets.pop()

    def binary(self, level):
        if level == 2:
            if self.at("NOT"):
                t = self.take()
                return self.node(Expr("NOT", (self.binary(2),), t.position))
            return self.comparison()
        operators = ("OR",) if level == 0 else ("AND",)
        left = self.binary(level + 1)
        while any(self.at(op) for op in operators):
            t = self.take()
            left = self.node(Expr(t.lexeme.upper(), (left, self.binary(level + 1)), t.position))
        return left

    def comparison(self):
        left = self.arithmetic(0)
        t = self.token
        if self.accept("IS"):
            negate = bool(self.accept("NOT"))
            self.expect("NULL")
            return self.node(Expr("IS NOT NULL" if negate else "IS NULL", (left,), t.position))
        negate = bool(self.accept("NOT"))
        if self.accept("BETWEEN"):
            low = self.arithmetic(0)
            self.expect("AND")
            right = self.arithmetic(0)
            result = self.node(Expr("BETWEEN", (left, low, right), t.position))
        elif self.accept("LIKE"):
            pattern = self.arithmetic(0)
            escape = self.arithmetic(0) if self.accept("ESCAPE") else None
            result = self.node(Expr("LIKE", (left, pattern) + ((escape,) if escape else ()), t.position))
        elif self.accept("IN"):
            self.expect("(")
            if self.at("SELECT"):
                args = (left, self.query())
            else:
                args = (left, *self.separated(lambda: self.binary(0)))
            self.expect(")")
            result = self.node(Expr("IN", args, t.position))
        else:
            if negate:
                self.fail("UNEXPECTED_TOKEN", "NOT 后要求 IN/LIKE/BETWEEN")
            if any(self.at(op) for op in ("=", "!=", "<>", "<", "<=", ">", ">=")):
                self.take()
                return self.node(Expr(t.lexeme, (left, self.arithmetic(0)), t.position))
            return left
        return self.node(Expr("NOT", (result,), t.position)) if negate else result

    def arithmetic(self, level):
        if level == 2:
            return self.primary()
        ops = ("+", "-") if level == 0 else ("*", "/")
        left = self.arithmetic(level + 1)
        while any(self.at(op) for op in ops):
            t = self.take()
            left = self.node(Expr(t.lexeme, (left, self.arithmetic(level + 1)), t.position))
        return left

    def primary(self):
        self.primary_depth += 1
        if self.primary_depth > 64:
            self.fail("EXPRESSION_TOO_COMPLEX", "表达式累计递归层数超过 64")
        try:
            return self._primary()
        finally:
            self.primary_depth -= 1

    def _primary(self):
        t = self.token
        if self.accept("("):
            if self.at("SELECT"):
                result = self.node(Expr("scalar", (self.query(),), t.position))
            else:
                result = self.binary(0)
            self.expect(")")
            return result
        if self.accept("EXISTS"):
            self.expect("(")
            result = self.node(Expr("EXISTS", (self.query(),), t.position))
            self.expect(")")
            return result
        if t.lexeme.upper() in AGGREGATES and t.type is TokenType.KEYWORD:
            self.take()
            self.expect("(")
            if self.accept("*"):
                args = ()
                if t.lexeme.upper() != "COUNT":
                    self.fail("UNEXPECTED_TOKEN", "只有 COUNT 支持 *")
            else:
                args = (self.binary(0),)
            self.expect(")")
            return self.node(Expr(t.lexeme.upper(), args, t.position))
        if self.accept("NULL"):
            return self.node(Expr("literal", (None,), t.position, TypeSpec("NULL")))
        if self.at("TRUE") or self.at("FALSE"):
            self.take()
            return self.node(Expr("literal", (t.lexeme.upper() == "TRUE",), t.position, TypeSpec("BOOL", nullable=False)))
        if t.lexeme.upper() in ("DATE", "TIME", "TIMESTAMP") and t.type is TokenType.KEYWORD:
            self.take()
            value = self.token
            if value.type is not TokenType.CONST or not value.lexeme.startswith("'"):
                self.fail("UNEXPECTED_TOKEN", "日期时间要求字符串字面量")
            self.take()
            raw = value.lexeme[1:-1]
            kind = t.lexeme.upper()
            patterns = {"DATE": r"\d{4}-\d{2}-\d{2}", "TIME": r"\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?", "TIMESTAMP": r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"}
            try:
                if not re.fullmatch(patterns[kind], raw):
                    raise ValueError()
                decoded = {"DATE": date, "TIME": time, "TIMESTAMP": datetime}[kind].fromisoformat(raw)
            except ValueError:
                self.fail("INVALID_LITERAL", "非法日期时间字面量", value.position)
            return self.node(Expr("literal", (decoded,), t.position, TypeSpec(kind, nullable=False)))
        if t.type is TokenType.IDENTIFIER or self.at("OLD") or self.at("NEW"):
            name = self.take().lexeme.lower()
            if self.accept("."):
                if self.accept("*"):
                    return self.node(Expr("star", (name,), t.position))
                return self.node(Expr("column", (name, self.name()), t.position))
            return self.node(Expr("column", (None, name), t.position))
        negative = bool(self.accept("-"))
        token = self.token
        if token.type is TokenType.CONST and token.lexeme[0:1].isdigit():
            self.take()
            raw = token.lexeme
            if "." in raw:
                digits = raw.replace(".", "").lstrip("0")
                if len(digits) > 38 or len(raw.split('.')[1]) > 38:
                    self.fail("NUMERIC_OUT_OF_RANGE", "小数字面量超过 38 位", token.position)
                value = Decimal(("-" if negative else "") + raw)
                scale = len(raw.split('.')[1])
                typ = TypeSpec("DECIMAL", max(len(digits), scale, 1), scale, False)
            else:
                raw = raw.lstrip('0') or '0'
                value = int(raw) if len(raw) <= 19 else 2**63 + 1
                value = -value if negative else value
                typ = TypeSpec("INT", nullable=False)
            return self.node(Expr("literal", (value,), t.position, typ))
        if negative:
            self.fail("UNEXPECTED_TOKEN", "负号后要求数值常量")
        if token.type is TokenType.CONST and token.lexeme.startswith("'"):
            self.take()
            return self.node(Expr("literal", (token.lexeme[1:-1].replace("''", "'"),), token.position, TypeSpec("VARCHAR", nullable=False)))
        self.fail("UNEXPECTED_TOKEN", "期望表达式")
