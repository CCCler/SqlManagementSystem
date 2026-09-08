"""Database：组装模块、按语句编译/执行/刷新、正常与异常退出均关闭存储。"""
from pathlib import Path

from minisql.compiler.compiler import SQLCompiler
from minisql.contracts.interfaces import CatalogWriter, Compiler, Executor, RecordStorage
from minisql.contracts.models import ExecutionResult
from minisql.engine.catalog import PersistentCatalog
from minisql.engine.executor import PlanExecutor
from minisql.storage.buffer import PageBufferPool
from minisql.storage.file_manager import FileManager
from minisql.storage.page import DiskPageManager
from minisql.storage.record import HeapStorage


class Database:
    def __init__(self, compiler: Compiler, executor: Executor,
                 catalog: CatalogWriter, storage: RecordStorage) -> None:
        self.compiler = compiler
        self.executor = executor
        self.catalog = catalog
        self.storage = storage

    def execute(self, sql: str) -> list[ExecutionResult]:
        """按语句编译、执行、刷新；遇错停止，之前成功操作保持有效，不返回部分结果。"""
        results: list[ExecutionResult] = []
        for statement in self.compiler.split_statements(sql):
            compiled = self.compiler.compile(statement, self.catalog)
            results.append(self.executor.execute(compiled.optimized_plan))
            self.storage.flush()
        return results

    def close(self) -> None:
        """刷新并关闭存储；刷新错误不允许被吞掉。"""
        self.storage.flush()
        self.storage.close()


def open_database(path: Path) -> Database:
    """组装 FileManager、DiskPageManager、PageBufferPool、HeapStorage、
    PersistentCatalog、SQLCompiler、PlanExecutor 和 Database，随后 bootstrap。"""
    path.mkdir(parents=True, exist_ok=True)
    # 数据文件命名待成员二的 FileManager 实现确定，联调（阶段 2）时校准。
    files = FileManager(path / "minisql.db")
    pages = DiskPageManager(files)
    buffer = PageBufferPool(pages)
    storage = HeapStorage(pages, buffer)
    catalog = PersistentCatalog(storage)
    database = Database(SQLCompiler(), PlanExecutor(storage, catalog), catalog, storage)
    catalog.bootstrap()
    return database
