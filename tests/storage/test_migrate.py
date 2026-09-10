"""F08/F06 存储格式迁移测试：V1→V2 识别、备份、重建、中断恢复与幂等。

覆盖 engine.migrate 的预演/执行、table_id 保留、旧数据与目录恢复、待处理日志与
损坏输入拒绝，以及子进程迁移中断后原库完好并可安全重跑。
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, TableSchema
from minisql.engine.catalog import PersistentCatalog
from minisql.engine.database import open_database
from minisql.engine.migrate import detect_format_version, migrate
from minisql.storage.buffer import PageBufferPool
from minisql.storage.file_manager import FileManager
from minisql.storage.page import (
    FORMAT_VERSION_V1, FORMAT_VERSION_V2, DiskPageManager, write_format_version,
)
from minisql.storage.record import HeapStorage

PROJECT = Path(__file__).resolve().parents[2]
DB = "minisql.db"

INT = ColumnSchema("id", DataType.INT)
NAME = ColumnSchema("name", DataType.VARCHAR)


def build_v1(directory, tables, drop=()):
    """写出一个 V1 格式库；tables 为 (表名, 列, 行) 列表，drop 为建好后删除的表名。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / DB
    pages = DiskPageManager(FileManager(path))
    data = bytearray(pages.read_page(0))
    write_format_version(data, 0)  # 旧文件 reserved=0，读取时归一化为 V1。
    pages.write_page(0, bytes(data))
    pages.close()

    pages = DiskPageManager(FileManager(path))
    storage = HeapStorage(pages, PageBufferPool(pages))
    try:
        catalog = PersistentCatalog(storage)
        catalog.bootstrap()
        for name, columns, rows in tables:
            created = storage.create_table(TableSchema(name, tuple(columns)))
            for row in rows:
                storage.insert(created, row)
            catalog.register_table(created)
        for name in drop:
            schema = catalog.get_table(name)
            catalog.unregister_table(schema.name)
            storage.drop_table(schema)
        storage.flush()
    finally:
        pages.close()
    return path


def engine_rows(directory, name):
    db = open_database(directory)
    try:
        return db.execute(f"SELECT * FROM {name};")[0].rows
    finally:
        db.close()


def test_detect_format_version_v1_and_v2(tmp_path):
    build_v1(tmp_path, [("t", [INT], [(1,)])])
    assert detect_format_version(tmp_path) == FORMAT_VERSION_V1
    migrate(tmp_path, apply=True)
    assert detect_format_version(tmp_path) == FORMAT_VERSION_V2


def test_dry_run_reports_without_changing_file(tmp_path):
    build_v1(tmp_path, [("t", [INT, NAME], [(1, "a"), (2, "b")])])
    before = (tmp_path / DB).read_bytes()

    report = migrate(tmp_path)

    assert report.changed and not report.applied
    assert report.source_version == FORMAT_VERSION_V1
    assert report.target_version == FORMAT_VERSION_V2
    assert (report.tables, report.rows) == (1, 2)
    assert (tmp_path / DB).read_bytes() == before
    assert detect_format_version(tmp_path) == FORMAT_VERSION_V1


def test_apply_migrates_preserving_tables_and_rows(tmp_path):
    build_v1(tmp_path, [
        ("users", [INT, NAME], [(1, "alice"), (2, "bob")]),
        ("logs", [INT, NAME], [(9, "hello"), (10, "世界")]),
    ])

    report = migrate(tmp_path, apply=True)

    assert report.applied and report.tables == 2 and report.rows == 4
    assert detect_format_version(tmp_path) == FORMAT_VERSION_V2
    assert engine_rows(tmp_path, "users") == ((1, "alice"), (2, "bob"))
    assert engine_rows(tmp_path, "logs") == ((9, "hello"), (10, "世界"))


def test_apply_creates_backup_of_original_bytes(tmp_path):
    build_v1(tmp_path, [("t", [INT], [(1,), (2,)])])
    original = (tmp_path / DB).read_bytes()

    report = migrate(tmp_path, apply=True)

    assert report.backup is not None
    assert report.backup.read_bytes() == original
    assert report.backup.name == DB + ".v1.bak"


def test_apply_is_idempotent(tmp_path):
    build_v1(tmp_path, [("t", [INT], [(1,)])])
    migrate(tmp_path, apply=True)
    migrated = (tmp_path / DB).read_bytes()

    again = migrate(tmp_path, apply=True)

    assert not again.changed and not again.applied
    assert (tmp_path / DB).read_bytes() == migrated
    assert engine_rows(tmp_path, "t") == ((1,),)


def test_apply_preserves_table_ids(tmp_path):
    build_v1(
        tmp_path,
        [("a", [INT], [(1,)]), ("b", [INT], [(2,)]), ("c", [INT], [(3,)])],
        drop=("b",),  # 留下编号空洞：a=1, c=3。
    )

    migrate(tmp_path, apply=True)

    db = open_database(tmp_path)
    try:
        ids = {schema.name: schema.table_id for schema in db.catalog.list_tables()}
    finally:
        db.close()
    assert ids == {"a": 1, "c": 3}


def test_refuses_pending_journal_without_touching_file(tmp_path):
    build_v1(tmp_path, [("t", [INT], [(1,)])])
    before = (tmp_path / DB).read_bytes()
    (tmp_path / "minisql.journal").write_bytes(b"pending")

    with pytest.raises(MiniSQLError, match="RECOVERY_REQUIRED"):
        migrate(tmp_path, apply=True)

    assert (tmp_path / DB).read_bytes() == before


def test_missing_database_rejected(tmp_path):
    with pytest.raises(MiniSQLError, match="IO_ERROR"):
        migrate(tmp_path, apply=True)


def test_corrupt_source_rejected_and_untouched(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    corrupt = bytes(4096)  # 长度足够但魔数无效，不得被静默重建。
    (tmp_path / DB).write_bytes(corrupt)

    with pytest.raises(MiniSQLError, match="CORRUPT_DATABASE"):
        migrate(tmp_path, apply=True)

    assert (tmp_path / DB).read_bytes() == corrupt


def test_interrupted_migration_keeps_original_and_reruns(tmp_path):
    build_v1(tmp_path, [("t", [INT, NAME], [(1, "a"), (2, "b")])])
    original = (tmp_path / DB).read_bytes()
    code = '''
import os, sys
from pathlib import Path
import minisql.engine.migrate as m
def crash(source, destination):
    os._exit(73)  # 模拟原子替换前进程崩溃。
m.durable_replace = crash
m.migrate(Path(sys.argv[1]), apply=True, backup=True)
'''
    child = subprocess.run([sys.executable, "-c", code, str(tmp_path)], cwd=PROJECT,
                           capture_output=True, text=True, timeout=20)
    assert child.returncode == 73, child.stderr

    assert (tmp_path / DB).read_bytes() == original
    assert detect_format_version(tmp_path) == FORMAT_VERSION_V1
    assert (tmp_path / (DB + ".migrating")).exists()  # 中断残留临时文件。

    report = migrate(tmp_path, apply=True)  # 重跑应清理残留并成功。
    assert report.applied and report.tables == 1 and report.rows == 2
    assert detect_format_version(tmp_path) == FORMAT_VERSION_V2
    assert engine_rows(tmp_path, "t") == ((1, "a"), (2, "b"))
