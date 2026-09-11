"""Database：组装模块、按语句编译/执行/刷新、正常与异常退出均关闭存储。"""
from pathlib import Path
import threading

from minisql.compiler.compiler import SQLCompiler
from minisql.compiler.lexer import Lexer
from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.models import TokenType
from minisql.storage.journal import DatabaseLock, RollbackJournal, sync_directory
from minisql.contracts.interfaces import CatalogWriter, Compiler, Executor, RecordStorage
from minisql.contracts.models import ExecutionResult
from minisql.engine.catalog import PersistentCatalog
from minisql.engine.executor import PlanExecutor
from minisql.engine.objects import PersistentAccountStore, PersistentObjectCatalog
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


def open_database(path: Path, *, lock_timeout: float = 5.0) -> "TransactionalDatabase":
    """打开支持自动提交、显式事务、进程锁和崩溃恢复的数据库。"""
    return TransactionalDatabase(Path(path), lock_timeout=lock_timeout)


def _transaction_error(code, reason):
    return MiniSQLError(ErrorStage.EXECUTION, code, reason)


class TransactionalDatabase(Database):
    """每个连接持有独立页缓存；事务持有数据库独占锁直到结束。

    完整前映像日志适用于教学规模数据库，空间及 BEGIN 成本与数据库大小成正比。
    并发保证仅适用于 execute/begin/commit/rollback；底层对象供模块测试使用。
    """

    def __init__(self, path: Path, *, lock_timeout: float = 5.0):
        self.path = path.resolve()
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock = DatabaseLock(self.path / "minisql.lock", lock_timeout)
        self._journal = RollbackJournal(self.path)
        self._guard = threading.RLock()
        self._files = None
        self._active = self._explicit = self._failed = self._broken = self._closed = False
        self._owner = None
        self.compiler = SQLCompiler()
        try:
            self._start()
            self._commit_locked()
        except BaseException:
            self.close()
            raise

    @property
    def in_transaction(self):
        return self._active and self._explicit

    def _reload(self):
        self._files = FileManager(self.path / "minisql.db")
        pages = DiskPageManager(self._files)
        self.storage = HeapStorage(pages, PageBufferPool(pages))
        self.catalog = PersistentCatalog(self.storage)
        self.executor = PlanExecutor(self.storage, self.catalog)
        self.catalog.bootstrap()
        self.objects = PersistentObjectCatalog(self.storage, self.catalog)
        self.accounts = PersistentAccountStore(self.storage, self.catalog)
        self.objects.bootstrap()
        self.accounts.bootstrap()

    def _close_file(self):
        if self._files is not None:
            self._files.close()
            self._files = None

    def _reset_transaction(self):
        self._active = self._explicit = self._failed = False
        self._owner = None
        self._lock.release()

    def _start(self):
        self._lock.acquire()
        try:
            self._close_file()
            self._journal.recover()
            self._journal.begin()  # 包括首次创建页 0 与系统表，所有写入均在日志之后。
            self._reload()  # 持锁后重新加载，不能使用其他连接提交前的旧缓存。
            self._active = True
            self._owner = threading.get_ident()
        except BaseException:
            self._close_file()
            try:
                self._journal.recover()
            finally:
                self._broken = True
                self._lock.release()
            raise

    def _commit_locked(self):
        self.storage.flush()
        self._files.sync()  # 数据先持久化，再写提交标记。
        sync_directory(self.path)
        try:
            self._journal.commit()
        except BaseException as error:
            self._broken = True
            self._close_file()
            self._reset_transaction()
            raise _transaction_error("COMMIT_UNCERTAIN", "提交标记写入未确认；请关闭并重新打开数据库核实结果") from error
        self._reset_transaction()

    def _rollback_locked(self):
        try:
            self._close_file()  # 不再将旧脏缓存写回恢复后的文件。
            self._journal.rollback()
            self._reload()
        except BaseException:
            self._broken = True
            raise
        finally:
            self._reset_transaction()

    def begin(self):
        return self.execute("BEGIN;")[0]

    def commit(self):
        return self.execute("COMMIT;")[0]

    def rollback(self):
        return self.execute("ROLLBACK;")[0]

    def _execute_one(self, statement):
        tokens = Lexer().tokenize(statement)
        control = tokens[0].type is TokenType.KEYWORD and tokens[0].lexeme.upper() in ("BEGIN", "COMMIT", "ROLLBACK")
        if control:
            action = self.compiler.compile(statement, self.catalog).plan.action
            if action == "ROLLBACK":
                if not self._active:
                    raise _transaction_error("NO_TRANSACTION", "当前没有事务")
                self._rollback_locked()
                return ExecutionResult(message="事务已回滚")
            if self._failed:
                raise _transaction_error("TRANSACTION_ABORTED", "事务已出错，必须先 ROLLBACK")
            if action == "BEGIN":
                if self._active:
                    raise _transaction_error("TRANSACTION_ACTIVE", "不支持嵌套事务")
                self._start()
                self._explicit = True
                return ExecutionResult(message="事务已开始")
            if not self._active:
                raise _transaction_error("NO_TRANSACTION", "当前没有事务")
            self._commit_locked()
            return ExecutionResult(message="事务已提交")
        if self._failed:
            raise _transaction_error("TRANSACTION_ABORTED", "事务已出错，必须先 ROLLBACK")
        automatic = not self._active
        if automatic:
            self._start()
        try:
            compiled = self.compiler.compile(statement, self.catalog)
            result = self.executor.execute(compiled.optimized_plan)
            if automatic:
                self._commit_locked()
            return result
        except BaseException:
            if automatic and self._active:
                self._rollback_locked()
            raise

    def execute(self, sql: str) -> list[ExecutionResult]:
        with self._guard:
            if self._closed or self._broken:
                raise _transaction_error("CONNECTION_CLOSED", "连接已关闭或不可继续使用，请重新打开")
            if self._active and self._owner != threading.get_ident():
                raise _transaction_error("TRANSACTION_OWNER", "显式事务必须由开始它的线程操作")
            try:
                return [self._execute_one(statement) for statement in self.compiler.split_statements(sql)]
            except BaseException as error:
                if self._active:
                    if isinstance(error, Exception):
                        self._failed = True
                    else:
                        self._rollback_locked()
                if isinstance(error, OSError):
                    raise MiniSQLError(ErrorStage.STORAGE, "IO_ERROR", str(error)) from error
                raise

    def close(self):
        with self._guard:
            if self._closed:
                return
            try:
                if self._active:
                    self._rollback_locked()
            finally:
                try:
                    self._close_file()
                finally:
                    self._closed = True
                    self._lock.close()
