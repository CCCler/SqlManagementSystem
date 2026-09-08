from minisql.contracts.interfaces import CatalogReader
from minisql.contracts.plans import CompilationResult
from minisql.compiler.lexer import Lexer
from minisql.compiler.parser import Parser
from minisql.compiler.semantic import SemanticAnalyzer
from minisql.compiler.planner import Planner
from minisql.compiler.optimizer import Optimizer
from minisql.compiler.statements import scan_statements


class SQLCompiler:
    def compile(self, sql: str, catalog: CatalogReader) -> CompilationResult:
        tokens = Lexer().tokenize(sql)
        ast = Parser().parse(tokens)
        semantic = SemanticAnalyzer().analyze(ast, catalog)
        plan = Planner().build(semantic)
        return CompilationResult(tokens, ast, semantic, plan, Optimizer().optimize(plan))

    def split_statements(self, sql: str) -> tuple[str, ...]:
        # 与 CLI 共用边界扫描，残缺尾部仍交给后续单语句编译报告错误。
        scanned = scan_statements(sql)
        fragments = []
        prefix = ""
        start = 0
        for end in scanned.ends:
            fragments.append(prefix + sql[start:end])
            prefix += "".join(c if c in "\r\n" else " " for c in sql[start:end])
            start = end
        if scanned.has_pending:
            fragments.append(prefix + sql[start:])
        return tuple(fragments)
