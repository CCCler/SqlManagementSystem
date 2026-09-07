from minisql.contracts.interfaces import CatalogReader
from minisql.contracts.plans import CompilationResult


class SQLCompiler:
    def compile(self, sql: str, catalog: CatalogReader) -> CompilationResult:
        raise NotImplementedError("成员一：串联单语句编译流水线")

    def split_statements(self, sql: str) -> tuple[str, ...]:
        # 返回片段保留前缀空白/换行，确保文件级行列位置不丢失。
        raise NotImplementedError("成员一：按词法状态切分，忽略字符串和注释内分号")
