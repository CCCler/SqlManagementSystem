from minisql.contracts.interfaces import CatalogReader
from minisql.contracts.plans import CompilationResult
from minisql.compiler.lexer import Lexer
from minisql.compiler.parser import Parser
from minisql.compiler.semantic import SemanticAnalyzer
from minisql.compiler.planner import Planner
from minisql.compiler.optimizer import Optimizer


class SQLCompiler:
    def compile(self, sql: str, catalog: CatalogReader) -> CompilationResult:
        tokens = Lexer().tokenize(sql)
        ast = Parser().parse(tokens)
        semantic = SemanticAnalyzer().analyze(ast, catalog)
        plan = Planner().build(semantic)
        return CompilationResult(tokens, ast, semantic, plan, Optimizer().optimize(plan))

    def split_statements(self, sql: str) -> tuple[str, ...]:
        # 不提前报告后续片段的词法错误，允许调用方逐条编译执行。
        fragments = []
        start = i = 0
        has_content = False
        prefix = ""
        while i < len(sql):
            if sql.startswith("--", i):
                end = sql.find("\n", i + 2)
                i = len(sql) if end == -1 else end
            elif sql.startswith("/*", i):
                end = sql.find("*/", i + 2)
                if end == -1:
                    has_content = True  # 未闭合注释交由 Lexer 定位。
                    i = len(sql)
                else:
                    i = end + 2
            elif sql[i] == "'":
                has_content = True
                i += 1
                while i < len(sql):
                    if sql[i] == "'":
                        i += 1
                        if i < len(sql) and sql[i] == "'":
                            i += 1
                            continue
                        break
                    i += 1
            elif sql[i] == ";":
                i += 1
                fragments.append(prefix + sql[start:i])
                prefix += "".join(c if c in "\r\n" else " " for c in sql[start:i])
                start, has_content = i, False
            else:
                has_content = has_content or not sql[i].isspace()
                i += 1
        if has_content:
            fragments.append(prefix + sql[start:])
        return tuple(fragments)
