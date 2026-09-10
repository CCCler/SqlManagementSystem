"""固定工作线程中的 GUI 会话，复用数据库执行和事务规则。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

from minisql.cli.trace import to_json_value
from minisql.contracts.errors import MiniSQLError
from minisql.engine.database import open_database


def error_value(error):
    if isinstance(error, MiniSQLError):
        return {"stage": error.stage.value, "code": error.code, "reason": error.reason,
                "position": to_json_value(error.position), "expected": list(error.expected)}
    return {"stage": "storage", "code": "IO_ERROR", "reason": str(error),
            "position": None, "expected": []}


class CaptureCompiler:
    def __init__(self, compiler):
        self.compiler = compiler
        self.events = []

    def split_statements(self, sql):
        return self.compiler.split_statements(sql)

    def compile(self, sql, catalog):
        compiled = self.compiler.compile(sql, catalog)
        self.events.append({"sql": sql.strip(), **{
            name: to_json_value(getattr(compiled, name)) for name in
            ("tokens", "ast", "semantic", "plan", "optimized_plan")}})
        return compiled


class Session:
    def __init__(self, default_path):
        self.path = Path(default_path).resolve()
        self.db = None
        self.failed = False
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="minisql-gui")
        self.guard = threading.Lock()
        self.pending = 0
        self.last_used = time.monotonic()
        self.closed = False

    def submit(self, action, body):
        with self.guard:
            if self.closed:
                raise ValueError("会话已结束，请刷新页面")
            self.pending += 1
            self.last_used = time.monotonic()
            future = self.worker.submit(self.call, action, body)
        future.add_done_callback(self._finished)
        return future

    def _finished(self, future):
        with self.guard:
            self.pending -= 1
            self.last_used = time.monotonic()

    def expired(self, now, timeout):
        with self.guard:
            return self.pending == 0 and now - self.last_used > timeout

    def state(self):
        return {"connected": self.db is not None, "directory": str(self.path),
                "tables": to_json_value(self.db.catalog.list_tables()) if self.db else [],
                "in_transaction": bool(self.db and self.db.in_transaction),
                "transaction_failed": self.failed}

    def _disconnect(self):
        try:
            if self.db:
                self.db.close()
        finally:
            self.db = None
            self.failed = False

    def call(self, action, body):
        try:
            if action == "connect":
                if self.db and self.db.in_transaction:
                    raise ValueError("切换连接前请先提交或回滚当前事务")
                directory = body.get("directory", str(self.path))
                if not isinstance(directory, str) or not directory.strip():
                    raise ValueError("请输入数据库目录")
                path = Path(directory).expanduser().resolve()
                candidate = open_database(path, lock_timeout=1.0)
                try:
                    self._disconnect()
                except BaseException:
                    candidate.close()
                    raise
                self.db, self.path = candidate, path
                self.db.compiler = CaptureCompiler(self.db.compiler)
            elif action == "disconnect":
                self._disconnect()
            elif action == "state":
                if self.db and not self.db.in_transaction:
                    # 引擎负责获取锁及重载目录；刷新不污染最近一次查询展示。
                    self.db.begin()
                    self.db.rollback()
            elif action == "execute":
                if not self.db:
                    raise ValueError("请先连接数据库")
                sql = body.get("sql")
                if not isinstance(sql, str) or not sql.strip():
                    raise ValueError("请输入 SQL 语句")
                return self.execute(sql)
            else:
                raise ValueError("未知操作")
            return {"ok": True, **self.state()}
        except (MiniSQLError, OSError) as error:
            return {"ok": False, "error": error_value(error), **self.state()}

    def execute(self, sql):
        self.db.compiler.events.clear()
        start = time.perf_counter()
        try:
            # 必须整批交给 execute，保持原有错误/部分结果契约及位置。
            results = self.db.execute(sql)
            payload = {"ok": True, "results": to_json_value(results)}
        except (MiniSQLError, OSError) as error:
            self.failed = self.db.in_transaction
            payload = {"ok": False, "results": [], "error": error_value(error)}
        if not self.db.in_transaction:
            self.failed = False
        pool = self.db.storage.buffer
        return {**payload, **self.state(), "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
                "compilations": self.db.compiler.events[:],
                "cache": {**to_json_value(pool.stats()), "writebacks": pool.writebacks,
                          "policy": pool.policy, "capacity": pool.capacity,
                          "replacement_log": to_json_value(pool.replacement_log())}}

    def close(self):
        with self.guard:
            if self.closed:
                return
            self.closed = True
            future = self.worker.submit(self._disconnect)
        try:
            future.result()
        finally:
            self.worker.shutdown(wait=True)
