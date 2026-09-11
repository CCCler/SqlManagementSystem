from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.models import SourcePosition, Token, TokenType


KEYWORDS = frozenset("CREATE TABLE INT VARCHAR INSERT INTO VALUES SELECT FROM WHERE DELETE UPDATE SET DROP BEGIN COMMIT ROLLBACK TRUE FALSE NOT AND OR EXPLAIN DISTINCT LIMIT ORDER BY ASC DESC OFFSET AS JOIN INNER LEFT RIGHT CROSS ON GROUP HAVING COUNT SUM AVG MAX MIN LIKE IN BETWEEN EXISTS ESCAPE IS NULL DECIMAL DATE TIME TIMESTAMP BOOL UNION ALL INTERSECT EXCEPT ALTER ADD COLUMN RENAME TO TYPE CONSTRAINT PRIMARY KEY UNIQUE FOREIGN REFERENCES CHECK DEFAULT RESTRICT NO ACTION INDEX VIEW TRIGGER AFTER FOR EACH ROW END OLD NEW USER IDENTIFIED GRANT REVOKE DATABASE".split())


class Lexer:
    def tokenize(self, sql: str) -> tuple[Token, ...]:
        tokens = []
        i, line, column = 0, 1, 1

        def advance(end: int) -> None:
            nonlocal i, line, column
            while i < end:
                if sql[i] == "\n":
                    line, column = line + 1, 1
                else:
                    column += 1
                i += 1

        while i < len(sql):
            start = i
            position = SourcePosition(line, column)
            char = sql[i]
            if char.isspace():
                advance(i + 1)
                continue
            if sql.startswith("--", i):
                end = sql.find("\n", i + 2)
                advance(len(sql) if end == -1 else end)
                continue
            if sql.startswith("/*", i):
                end = sql.find("*/", i + 2)
                if end == -1:
                    raise MiniSQLError(ErrorStage.LEXICAL, "UNCLOSED_COMMENT", "块注释未闭合", position)
                advance(end + 2)
                continue
            if char == "'":
                end = i + 1
                while end < len(sql):
                    if sql[end] == "'":
                        if end + 1 < len(sql) and sql[end + 1] == "'":
                            end += 2
                            continue
                        break
                    end += 1
                if end == len(sql):
                    raise MiniSQLError(ErrorStage.LEXICAL, "UNCLOSED_STRING", "字符串未闭合", position)
                advance(end + 1)
                kind = TokenType.CONST
            elif char.isascii() and (char.isalpha() or char == "_"):
                end = i + 1
                while end < len(sql) and sql[end].isascii() and (sql[end].isalnum() or sql[end] == "_"):
                    end += 1
                advance(end)
                kind = TokenType.KEYWORD if sql[start:i].upper() in KEYWORDS else TokenType.IDENTIFIER
            elif "0" <= char <= "9":
                end = i + 1
                while end < len(sql) and "0" <= sql[end] <= "9":
                    end += 1
                if end + 1 < len(sql) and sql[end] == "." and "0" <= sql[end + 1] <= "9":
                    end += 1
                    while end < len(sql) and "0" <= sql[end] <= "9":
                        end += 1
                advance(end)
                kind = TokenType.CONST
            elif char in "(),;.":
                advance(i + 1)
                kind = TokenType.DELIMITER
            elif sql[i:i + 2] in ("!=", "<>", "<=", ">="):
                advance(i + 2)
                kind = TokenType.OPERATOR
            elif char in "=<>+-*/":
                advance(i + 1)
                kind = TokenType.OPERATOR
            else:
                raise MiniSQLError(ErrorStage.LEXICAL, "INVALID_CHARACTER", f"非法字符：{char}", position)
            lexeme = sql[start:i]
            # 用户管理语句中的字符串全部隐藏，包括语法错误后的额外字符串。
            sensitive = any(tokens[j].lexeme.upper() in ("CREATE", "ALTER") and tokens[j+1].lexeme.upper() == "USER" for j in range(len(tokens)-1))
            secret = lexeme if sensitive and kind is TokenType.CONST and lexeme.startswith("'") else None
            tokens.append(Token(kind, "'<redacted>'" if secret is not None else lexeme, position, secret))
        tokens.append(Token(TokenType.EOF, "", SourcePosition(line, column)))
        return tuple(tokens)
