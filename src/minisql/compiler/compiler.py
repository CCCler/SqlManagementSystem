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
        from minisql.compiler.catalog import ReadOnlyCatalogAdapter
        catalog = ReadOnlyCatalogAdapter(catalog)
        tokens = Lexer().tokenize(sql)
        catalog.positions = {t.lexeme.lower(): t.position for t in reversed(tokens)}
        ast = Parser().parse(tokens)
        from minisql.compiler.capabilities import needs_extended
        from minisql.contracts.ast import ExplainStmt
        subject = ast.statement if isinstance(ast, ExplainStmt) else ast
        name = getattr(getattr(subject, "table", None), "name", None)
        if name:
            table = catalog.get_table(name)
            view_reader = getattr(catalog, "get_view", None)
            index_reader = getattr(catalog, "list_indexes", None)
            if needs_extended(table) or table is None and view_reader and view_reader(name) is not None or index_reader and index_reader(name):
                from minisql.compiler.extended_parser import ExtendedParser
                ast = ExtendedParser(tokens).parse()
                from minisql.compiler.parser import validate_ast_budget
                validate_ast_budget(ast)
        semantic = SemanticAnalyzer().analyze(ast, catalog)
        plan = Planner().build(semantic)
        from minisql.contracts.extensions import ExtendedPlan
        if isinstance(plan, ExtendedPlan):
            from minisql.compiler.extended_optimizer import optimize, collect_capabilities
            optimized = optimize(plan, catalog)
            return CompilationResult(tokens, ast, semantic, plan, optimized, optimized.output,
                                     optimized.dependencies, collect_capabilities(optimized))
        from minisql.contracts.extensions import OutputField, ObjectDependency, column_type
        from minisql.contracts.ast import SelectStmt
        output = ()
        dependencies = ()
        if semantic.schema is not None:
            dependencies = (ObjectDependency('table', semantic.schema.name),)
            if isinstance(subject, SelectStmt):
                selected = plan
                while not hasattr(selected, 'columns') and hasattr(selected, 'source'):
                    selected = selected.source
                names = getattr(selected, 'columns', ())
                fields = {c.name: c for c in semantic.schema.columns}
                output = tuple(OutputField(n, column_type(fields[n])) for n in names)
        return CompilationResult(tokens, ast, semantic, plan, Optimizer().optimize(plan), output, dependencies)

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
