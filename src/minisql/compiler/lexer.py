from minisql.contracts.models import Token


class Lexer:
    def tokenize(self, sql: str) -> tuple[Token, ...]:
        raise NotImplementedError("成员一：实现词法分析及行列定位")
