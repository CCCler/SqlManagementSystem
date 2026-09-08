"""数据库级操作系统锁及写前回滚日志；日志保存完整提交前映像。"""
import errno
import hashlib
import math
import os
from pathlib import Path
import struct
import time

from minisql.contracts.errors import ErrorStage, MiniSQLError


HEADER = struct.Struct(">8sQ32s")
MAGIC = b"MSQLUNDO"
COMMITTED = b"MSQL-COMMITTED\n"


def _error(code, reason):
    return MiniSQLError(ErrorStage.STORAGE, code, reason)


def sync_directory(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def durable_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move.restype = wintypes.BOOL
        # REPLACE_EXISTING | WRITE_THROUGH：发布日志后才允许修改数据库。
        if not move(str(source), str(destination), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, destination)
        sync_directory(destination.parent)


class DatabaseLock:
    def __init__(self, path: Path, timeout: float = 5.0):
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("锁等待时间必须是有限非负数")
        self.timeout = timeout
        self.file = open(path, "a+b", buffering=0)
        if path.stat().st_size == 0:
            self.file.write(b"\0")
        self.held = False

    def acquire(self):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.file.seek(0)
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.held = True
                return
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise _error("DATABASE_BUSY", "数据库被其他事务占用，请在其提交或回滚后重试") from error
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))

    def release(self):
        if not self.held:
            return
        if os.name == "nt":
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.held = False

    def close(self):
        try:
            self.release()
        finally:
            self.file.close()


class RollbackJournal:
    def __init__(self, directory: Path):
        self.database = directory / "minisql.db"
        self.path = directory / "minisql.journal"
        self.temp = directory / "minisql.journal.tmp"

    def begin(self):
        if self.path.exists():
            raise _error("RECOVERY_REQUIRED", "存在未处理的事务日志")
        before = self.database.read_bytes() if self.database.exists() else b""
        with open(self.temp, "wb") as stream:
            stream.write(HEADER.pack(MAGIC, len(before), hashlib.sha256(before).digest()))
            stream.write(before)
            stream.flush()
            os.fsync(stream.fileno())
        durable_replace(self.temp, self.path)

    def _read(self):
        data = self.path.read_bytes()
        if len(data) < HEADER.size:
            raise _error("CORRUPT_JOURNAL", "事务日志头不完整，保留文件以便检查")
        magic, size, digest = HEADER.unpack(data[:HEADER.size])
        before = data[HEADER.size:HEADER.size + size]
        trailer = data[HEADER.size + size:]
        if magic != MAGIC or len(before) != size or hashlib.sha256(before).digest() != digest:
            raise _error("CORRUPT_JOURNAL", "事务日志校验失败，未修改数据库")
        if not COMMITTED.startswith(trailer):
            raise _error("CORRUPT_JOURNAL", "事务日志提交标记损坏，未修改数据库")
        return before, trailer == COMMITTED

    def _restore(self, before: bytes):
        # 日志在恢复完整写入并同步前一直保留，恢复中再次中断仍可重试。
        with open(self.database, "w+b") as stream:
            stream.write(before)
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(self.database.parent)

    def _remove(self):
        self.path.unlink()
        sync_directory(self.path.parent)

    def recover(self):
        if not self.path.exists():
            return
        before, committed = self._read()
        if not committed:
            self._restore(before)
        self._remove()

    def rollback(self):
        before, committed = self._read()
        if committed:
            raise _error("COMMIT_UNCERTAIN", "日志已有提交标记，请关闭并重新打开数据库确认结果")
        self._restore(before)
        self._remove()

    def _mark_committed(self):
        with open(self.path, "ab") as stream:
            stream.write(COMMITTED)
            stream.flush()
            os.fsync(stream.fileno())

    def commit(self):
        # 调用者已同步数据文件；完整提交标记是提交决定。
        self._mark_committed()
        try:
            self._remove()
        except OSError:
            # 提交已持久化；清理失败由下次持锁恢复处理，不能误报已回滚。
            pass
