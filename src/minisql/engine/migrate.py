"""V1→V2 存储格式迁移（成员二）。

设计要点
--------
- 不自动转换业务目录：只有显式调用或 CLI ``--apply`` 才执行，且默认先备份原文件。
- 原子性：在临时文件中按 V2 重建全部用户表与 ``__catalog``，fsync 后用
  ``durable_replace`` 原子替换 ``minisql.db``；迁移中断时原库文件完好，可安全重跑。
- 保留 table_id：源表编号原样复制到新文件，避免上层元数据（如未来索引登记）失效。
- 保留系统表：方案 B 下 ``__views``/``__indexes``/``__users`` 等系统表与用户表同存
  ``__catalog``，一并重建，避免视图/索引/账户元数据在迁移后丢失。
- 版本识别：读页 0 页头 reserved 字节，历史文件的 0 归一化为 V1；已是 V2 时不动作。

本模块复用 ``PersistentCatalog`` 枚举表结构，因此位于 engine 层（engine 依赖
storage），避免 storage 反向依赖引擎；页面级格式仍由 storage 层定义。
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.models import TableSchema
from minisql.engine.catalog import PersistentCatalog
from minisql.storage.buffer import PageBufferPool
from minisql.storage.file_manager import FileManager
from minisql.storage.journal import DatabaseLock, durable_replace, sync_directory
from minisql.storage.page import (
    FORMAT_VERSION_CURRENT, FORMAT_VERSION_V2, HEADER_SIZE,
    META_OFFSET, META_SIZE, PAGE_SIZE, DiskPageManager, decode_meta, encode_meta,
    read_format_version,
)
from minisql.storage.record import HeapStorage

DATABASE_NAME = "minisql.db"
BACKUP_SUFFIX = ".v1.bak"
TEMP_SUFFIX = ".migrating"


def _error(code: str, reason: str) -> MiniSQLError:
    return MiniSQLError(ErrorStage.STORAGE, code, reason)


@dataclass(frozen=True)
class MigrationReport:
    """一次迁移或预演的结果；dry-run 时 applied=False。"""

    directory: Path
    source_version: int
    target_version: int
    tables: int
    rows: int
    changed: bool
    applied: bool
    backup: Path | None


def detect_format_version(directory) -> int:
    """读取数据库文件的格式版本；文件缺失或不完整时报存储错误。"""
    database = Path(directory) / DATABASE_NAME
    if not database.exists():
        raise _error("IO_ERROR", f"{database} 不存在")
    with open(database, "rb") as stream:
        head = stream.read(PAGE_SIZE)
    if len(head) < HEADER_SIZE:
        raise _error("CORRUPT_DATABASE", "数据库文件不完整，拒绝识别格式")
    return read_format_version(head)


def migrate(directory, *, apply: bool = False, backup: bool = True) -> MigrationReport:
    """把目录中的 V1 数据库迁移为 V2。

    apply=False（默认）只统计并返回报告，不改动任何文件；apply=True 时先备份再
    原子替换。目录被其他连接占用、存在未处理事务日志或数据损坏时拒绝迁移。
    """
    directory = Path(directory)
    database = directory / DATABASE_NAME
    if not database.exists():
        raise _error("IO_ERROR", f"{database} 不存在")

    version = detect_format_version(directory)
    if version >= FORMAT_VERSION_CURRENT:
        return MigrationReport(directory, version, version, 0, 0, False, False, None)

    lock = DatabaseLock(directory / "minisql.lock")
    lock.acquire()
    try:
        _reject_pending_journal(directory)
        version = detect_format_version(directory)  # 持锁后复核，避免并发迁移。
        if version >= FORMAT_VERSION_CURRENT:
            return MigrationReport(directory, version, version, 0, 0, False, False, None)

        if not apply:
            tables, rows = _survey(database)
            return MigrationReport(directory, version, FORMAT_VERSION_V2,
                                   tables, rows, True, False, None)

        backup_path = None
        if backup:
            backup_path = Path(str(database) + BACKUP_SUFFIX)
            _copy_durable(database, backup_path)

        temp = Path(str(database) + TEMP_SUFFIX)
        if temp.exists():
            temp.unlink()  # 清理上次中断留下的临时文件。
        try:
            tables, rows = _rebuild(database, temp)
            _fsync_file(temp)
            durable_replace(temp, database)
        except BaseException:
            if temp.exists():
                temp.unlink()
            raise
        sync_directory(directory)
        return MigrationReport(directory, version, FORMAT_VERSION_V2,
                               tables, rows, True, True, backup_path)
    finally:
        lock.close()


def _reject_pending_journal(directory: Path) -> None:
    if (directory / "minisql.journal").exists():
        raise _error("RECOVERY_REQUIRED",
                     "存在未处理的事务日志，请先正常打开数据库完成恢复后再迁移")


@contextmanager
def _open_storage(database: Path):
    """按文件当前格式版本打开存储；退出时刷新并关闭文件。"""
    pages = DiskPageManager(FileManager(database))
    try:
        storage = HeapStorage(pages, PageBufferPool(pages))
        yield storage
    finally:
        pages.close()


def _object_schemas(catalog: PersistentCatalog) -> list[TableSchema]:
    """枚举需迁移的全部对象表：用户表 + 已登记的系统表。

    不能直接用 ``list_tables()``——按契约它只返回用户表，会漏掉系统表，
    导致视图/索引/账户元数据迁移后丢失。``__catalog``（table_id=0）由
    ``bootstrap`` 在目标库自动重建，不在此列。
    """
    return [schema for schema in catalog.tables.values() if schema.table_id != 0]


def _survey(database: Path) -> tuple[int, int]:
    with _open_storage(database) as storage:
        catalog = PersistentCatalog(storage)
        catalog.bootstrap()
        schemas = _object_schemas(catalog)
        rows = sum(1 for schema in schemas for _ in storage.scan(schema))
    return len(schemas), rows


def _rebuild(source: Path, dest: Path) -> tuple[int, int]:
    """把 source 的全部对象表（用户表 + 系统表）与目录重建到 dest，返回 (表数, 行数)。"""
    with _open_storage(source) as src_storage:
        src_catalog = PersistentCatalog(src_storage)
        src_catalog.bootstrap()
        schemas = sorted(_object_schemas(src_catalog), key=lambda s: s.table_id)

        with _open_storage(dest) as dest_storage:
            dest_catalog = PersistentCatalog(dest_storage)
            dest_catalog.bootstrap()  # 建 table_id=0 的 __catalog（V2）。
            rows = 0
            for schema in schemas:
                _reserve_table_id(dest_storage, schema.table_id)
                created = dest_storage.create_table(TableSchema(schema.name, schema.columns))
                for record in src_storage.scan(schema):
                    dest_storage.insert(created, record.row)
                    rows += 1
                dest_catalog.register_table(created)
            dest_storage.flush()
    return len(schemas), rows


def _reserve_table_id(storage: HeapStorage, table_id: int) -> None:
    """把目标库的 next_table_id 置为 table_id，使下一张表沿用源编号。"""
    data = bytearray(storage.pages.read_page(0))
    meta = decode_meta(bytes(data))
    data[META_OFFSET:META_OFFSET + META_SIZE] = encode_meta(
        replace(meta, next_table_id=table_id))
    storage.pages.write_page(0, bytes(data))


def _copy_durable(source: Path, destination: Path) -> None:
    data = source.read_bytes()
    with open(destination, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(destination.parent)


def _fsync_file(path: Path) -> None:
    with open(path, "rb+") as stream:
        stream.flush()
        os.fsync(stream.fileno())
