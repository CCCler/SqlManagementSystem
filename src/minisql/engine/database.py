from pathlib import Path
from minisql.contracts.interfaces import CatalogWriter, Compiler, Executor, RecordStorage
from minisql.contracts.models import ExecutionResult


class Database:
    def __init__(self, compiler: Compiler, executor: Executor,
                 catalog: CatalogWriter, storage: RecordStorage) -> None:
        self.compiler = compiler
        self.executor = executor
        self.catalog = catalog
        self.storage = storage

    def execute(self, sql: str) -> list[ExecutionResult]:
        raise NotImplementedError("成员三：按语句编译、执行、刷新，遇错停止")

    def close(self) -> None:
        raise NotImplementedError("成员三：关闭数据库并刷新存储")


def open_database(path: Path) -> Database:
    raise NotImplementedError("成员三：组装模块并恢复 Catalog")
