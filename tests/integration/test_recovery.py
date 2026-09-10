"""真实子进程在提交及恢复关键位置退出。"""
from pathlib import Path
import subprocess
import sys

import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.engine.database import open_database


PROJECT = Path(__file__).resolve().parents[2]


def run_child(code, path):
    return subprocess.run([sys.executable, "-c", code, str(path)], cwd=PROJECT,
                          capture_output=True, text=True, timeout=15)


def seed(path):
    db = open_database(path)
    db.execute("CREATE TABLE t(id INT); INSERT INTO t(id) VALUES (1);")
    db.close()


CRASH = '''
import os, sys
from pathlib import Path
from minisql.engine.database import open_database
db = open_database(Path(sys.argv[1]))
db.execute("BEGIN; DELETE FROM t; INSERT INTO t(id) VALUES (2); CREATE TABLE extra(id INT);")
'''


@pytest.mark.parametrize("phase,committed", [
    ("uncommitted", False), ("before_marker", False),
    ("partial_marker", False), ("after_marker", True), ("after_commit", True),
])
def test_process_crash_atomic_commit_boundary(tmp_path, phase, committed):
    seed(tmp_path)
    endings = {
        "uncommitted": "db.storage.flush(); os._exit(71)",
        "before_marker": "db._journal._mark_committed = lambda: os._exit(71)\ndb.commit()",
        "partial_marker": '''
def partial():
    with open(db._journal.path, "ab") as file:
        file.write(b"MSQL-COM")
        file.flush()
        os.fsync(file.fileno())
    os._exit(71)
db._journal._mark_committed = partial
db.commit()
''',
        "after_marker": "db._journal._remove = lambda: os._exit(71)\ndb.commit()",
        "after_commit": "db.commit(); os._exit(71)",
    }
    child = run_child(CRASH + endings[phase], tmp_path)
    assert child.returncode == 71, child.stderr
    db = open_database(tmp_path)
    try:
        assert db.execute("SELECT * FROM t;")[0].rows == (((2,),) if committed else ((1,),))
        assert (db.catalog.get_table("extra") is not None) == committed
        assert not (tmp_path / "minisql.journal").exists()
    finally:
        db.close()


INTERRUPT_RECOVERY = '''
import os, sys
from pathlib import Path
from minisql.storage.journal import RollbackJournal
from minisql.engine.database import open_database
def interrupted(self, before):
    with open(self.database, "wb") as stream:
        stream.write(before[:100])
        stream.flush()
        os.fsync(stream.fileno())
    os._exit(72)
RollbackJournal._restore = interrupted
open_database(Path(sys.argv[1]))
'''


def test_recovery_can_itself_be_interrupted(tmp_path):
    seed(tmp_path)
    assert run_child(CRASH + "db.storage.flush(); os._exit(71)", tmp_path).returncode == 71
    assert run_child(INTERRUPT_RECOVERY, tmp_path).returncode == 72
    assert (tmp_path / "minisql.db").stat().st_size == 100
    db = open_database(tmp_path)
    try:
        assert db.execute("SELECT * FROM t;")[0].rows == ((1,),)
        assert db.catalog.get_table("extra") is None
    finally:
        db.close()


def test_recovery_interrupted_twice_then_completes(tmp_path):
    """恢复过程连续两次中断后仍可完成，且不残留日志。"""
    seed(tmp_path)
    assert run_child(CRASH + "db.storage.flush(); os._exit(71)", tmp_path).returncode == 71
    assert run_child(INTERRUPT_RECOVERY, tmp_path).returncode == 72
    assert run_child(INTERRUPT_RECOVERY, tmp_path).returncode == 72
    assert (tmp_path / "minisql.db").stat().st_size == 100

    db = open_database(tmp_path)
    try:
        assert db.execute("SELECT * FROM t;")[0].rows == ((1,),)
        assert db.catalog.get_table("extra") is None
    finally:
        db.close()
    assert not (tmp_path / "minisql.journal").exists()


@pytest.mark.parametrize("damage", ["truncate", "checksum", "marker"])
def test_corrupt_journal_fails_without_overwriting_data(tmp_path, damage):
    seed(tmp_path)
    assert run_child(CRASH + "db.storage.flush(); os._exit(71)", tmp_path).returncode == 71
    journal = tmp_path / "minisql.journal"
    raw = bytearray(journal.read_bytes())
    if damage == "truncate":
        raw = raw[:10]
    elif damage == "checksum":
        raw[-1] ^= 1
    else:
        raw += b"INVALID"
    journal.write_bytes(raw)
    database_bytes = (tmp_path / "minisql.db").read_bytes()
    with pytest.raises(MiniSQLError, match="CORRUPT_JOURNAL"):
        open_database(tmp_path)
    assert journal.read_bytes() == raw
    assert (tmp_path / "minisql.db").read_bytes() == database_bytes


@pytest.mark.parametrize("invalid", [b"bad", b"x" * 4096])
def test_bad_database_not_silently_reinitialized(tmp_path, invalid):
    (tmp_path / "minisql.db").write_bytes(invalid)
    with pytest.raises(MiniSQLError, match="CORRUPT_DATABASE"):
        open_database(tmp_path)
    assert (tmp_path / "minisql.db").read_bytes() == invalid


@pytest.mark.parametrize("marked", [False, True])
def test_commit_io_error_is_uncertain_not_false_rollback(tmp_path, monkeypatch, marked):
    seed(tmp_path)
    db = open_database(tmp_path)
    db.execute("BEGIN; INSERT INTO t(id) VALUES (2);")
    original = db._journal._mark_committed

    def fail():
        if marked:
            original()
        raise OSError("模拟提交标记同步失败")
    monkeypatch.setattr(db._journal, "_mark_committed", fail)
    with pytest.raises(MiniSQLError, match="COMMIT_UNCERTAIN"):
        db.commit()
    with pytest.raises(MiniSQLError, match="CONNECTION_CLOSED"):
        db.execute("SELECT * FROM t;")
    db.close()
    reopened = open_database(tmp_path)
    try:
        expected = ((1,), (2,)) if marked else ((1,),)
        assert reopened.execute("SELECT * FROM t;")[0].rows == expected
    finally:
        reopened.close()


def test_journal_is_durable_before_statement_writes(tmp_path, monkeypatch):
    seed(tmp_path)
    db = open_database(tmp_path)
    before = (tmp_path / "minisql.db").read_bytes()
    def fail():
        raise OSError("日志不可写")
    monkeypatch.setattr(db._journal, "begin", fail)
    with pytest.raises(MiniSQLError, match="IO_ERROR"):
        db.execute("INSERT INTO t(id) VALUES (2);")
    db.close()
    assert (tmp_path / "minisql.db").read_bytes() == before


def test_independent_processes_serialize_read_modify_write(tmp_path):
    db = open_database(tmp_path)
    db.execute("CREATE TABLE counter(value INT); INSERT INTO counter(value) VALUES (0);")
    db.close()
    code = '''
import sys
from pathlib import Path
from minisql.engine.database import open_database
db = open_database(Path(sys.argv[1]), lock_timeout=10)
try:
    for i in range(8):
        db.begin()
        value = db.execute("SELECT value FROM counter;")[0].rows[0][0]
        db.execute(f"DELETE FROM counter; INSERT INTO counter(value) VALUES ({value+1});")
        db.commit()
finally:
    db.close()
'''
    children = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], cwd=PROJECT,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
    try:
        for child in children:
            stdout, stderr = child.communicate(timeout=20)
            assert child.returncode == 0, (stdout, stderr)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    db = open_database(tmp_path)
    try:
        assert db.execute("SELECT * FROM counter;")[0].rows == ((24,),)
    finally:
        db.close()


def test_other_process_cannot_read_uncommitted_rows(tmp_path):
    seed(tmp_path)
    db = open_database(tmp_path)
    db.execute("BEGIN; INSERT INTO t(id) VALUES (2);")
    code = '''
import sys
from pathlib import Path
from minisql.engine.database import open_database
from minisql.contracts.errors import MiniSQLError
try:
    db = open_database(Path(sys.argv[1]), lock_timeout=0.05)
except MiniSQLError as error:
    assert error.code == "DATABASE_BUSY"
    sys.exit(0)
raise AssertionError("未获得锁不应能打开读取数据库")
'''
    try:
        child = run_child(code, tmp_path)
        assert child.returncode == 0, child.stderr
    finally:
        db.close()


@pytest.mark.parametrize("commit", [False, True])
def test_drop_recreate_crash_recovers_catalog_and_reused_pages(tmp_path, commit):
    seed(tmp_path)
    code = '''
import os, sys
from pathlib import Path
from minisql.engine.database import open_database
db = open_database(Path(sys.argv[1]))
db.execute("BEGIN; DROP TABLE t; CREATE TABLE t(name VARCHAR); INSERT INTO t(name) VALUES ('new');")
'''
    code += "db.commit(); os._exit(71)" if commit else "db.storage.flush(); os._exit(71)"
    child = run_child(code, tmp_path)
    assert child.returncode == 71, child.stderr
    db = open_database(tmp_path)
    try:
        result = db.execute("SELECT * FROM t;")[0]
        assert result.columns == (("name",) if commit else ("id",))
        assert result.rows == ((("new",),) if commit else ((1,),))
    finally:
        db.close()
