from minisql.contracts.ast import Statement
from minisql.contracts.models import Token


class Parser:
    def parse(self, tokens: tuple[Token, ...]) -> Statement:
        raise NotImplementedError("成员一：实现单语句递归下降分析")
