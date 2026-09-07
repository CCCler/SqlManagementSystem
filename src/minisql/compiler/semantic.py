from minisql.contracts.ast import Statement
from minisql.contracts.interfaces import CatalogReader
from minisql.contracts.plans import SemanticResult


class SemanticAnalyzer:
    def analyze(self, statement: Statement, catalog: CatalogReader) -> SemanticResult:
        raise NotImplementedError("成员一：实现名字绑定和类型检查")
