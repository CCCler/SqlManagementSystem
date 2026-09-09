"""旧页格式与优化错误语义回归，全部使用隔离文件或内存替身。"""
from copy import deepcopy
from dataclasses import replace
import struct

import pytest

from minisql.compiler.compiler import SQLCompiler
from minisql.contracts.errors import MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType, TableSchema
from minisql.contracts.plans import EmptyScan, Filter
from minisql.engine.database import open_database
from minisql.engine.executor import PlanExecutor
from minisql.storage.buffer import PageBufferPool
from minisql.storage.file_manager import FileManager
from minisql.storage.page import DiskPageManager, PAGE_SIZE, HEADER_SIZE, decode_header, encode_header
from minisql.storage.record import HeapStorage
from tests.fakes.memory import MemoryCatalog, MemoryStorage


@pytest.mark.parametrize("predicate", [
    "id+1>0 AND FALSE", "FALSE AND id+1>0",
    "id+1>0 OR TRUE", "TRUE OR id+1>0",
    "NOT (FALSE AND id+1>0)",
    "(id+1>0 OR TRUE) AND FALSE",
    "9223372036854775807+1>0 AND FALSE",
    "TRUE OR -9223372036854775808-1<0",
])
@pytest.mark.parametrize("statement", ["SELECT * FROM t WHERE ", "DELETE FROM t WHERE "])
def test_optimization_preserves_overflow_errors(predicate, statement):
    storage, catalog = MemoryStorage(), MemoryCatalog()
    executor, compiler = PlanExecutor(storage, catalog), SQLCompiler()
    for sql in ("CREATE TABLE t(id INT);", "INSERT INTO t(id) VALUES (9223372036854775807);"):
        executor.execute(compiler.compile(sql, catalog).optimized_plan)
    compiled = compiler.compile(statement + predicate + ";", catalog)
    before = deepcopy(compiled.plan)
    for plan in (compiled.plan, compiled.optimized_plan):
        with pytest.raises(MiniSQLError) as caught:
            executor.execute(plan)
        assert caught.value.code == "INTEGER_OUT_OF_RANGE"
    assert compiled.plan == before


def test_safe_constants_still_optimize_without_scanning():
    catalog = MemoryCatalog((TableSchema("t", (ColumnSchema("id", DataType.INT),), 1),))
    plan = SQLCompiler().compile("SELECT * FROM t WHERE 1+1>0 AND FALSE;", catalog).optimized_plan
    assert isinstance(plan.source, EmptyScan)


def test_empty_table_does_not_evaluate_retained_overflow():
    db_storage, catalog = MemoryStorage(), MemoryCatalog()
    executor, compiler = PlanExecutor(db_storage, catalog), SQLCompiler()
    executor.execute(compiler.compile("CREATE TABLE t(id INT);", catalog).optimized_plan)
    compiled = compiler.compile("SELECT * FROM t WHERE 9223372036854775807+1>0 AND FALSE;", catalog)
    assert isinstance(compiled.optimized_plan.source, Filter)
    assert executor.execute(compiled.plan).rows == executor.execute(compiled.optimized_plan).rows == ()


def test_overflow_delete_rolls_back_previous_rows(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT); INSERT INTO t(id) VALUES (1);"
                   "INSERT INTO t(id) VALUES (9223372036854775807);")
        with pytest.raises(MiniSQLError, match="INTEGER_OUT_OF_RANGE"):
            db.execute("DELETE FROM t WHERE TRUE OR id+1>0;")
        assert db.execute("SELECT * FROM t;")[0].rows == ((1,), (9223372036854775807,))
        db.begin()
        with pytest.raises(MiniSQLError, match="INTEGER_OUT_OF_RANGE"):
            db.execute("SELECT * FROM t WHERE FALSE AND id+1>0;")
        with pytest.raises(MiniSQLError, match="TRANSACTION_ABORTED"):
            db.commit()
        db.rollback()
    finally:
        db.close()


def make_legacy(path):
    db = open_database(path)
    try:
        db.execute("CREATE TABLE t(id INT, name VARCHAR); CREATE TABLE keep(id INT);")
        for i in range(3):
            db.execute(f"INSERT INTO t(id,name) VALUES ({i},'{str(i) * 2600}');")
        db.execute("INSERT INTO keep(id) VALUES (9);")
        schema = db.catalog.get_table("t")
        ids = [r.record_id for r in db.storage.scan(schema)]
    finally:
        db.close()
    # 使用旧版 >IBHHHii5s 编码，所有数据页的原 next_free_page 为 -1。
    raw = bytearray((path / "minisql.db").read_bytes())
    old_header = struct.Struct(">IBHHHii5s")
    for offset in range(PAGE_SIZE, len(raw), PAGE_SIZE):
        h = decode_header(raw[offset:offset + HEADER_SIZE])
        raw[offset:offset + HEADER_SIZE] = old_header.pack(
            h.page_id, h.page_type, h.slot_count, h.free_start, h.data_end,
            -1, h.next_data_page, bytes(5))
    (path / "minisql.db").write_bytes(raw)
    return schema, ids


def test_legacy_delete_rollback_commit_and_reopen(tmp_path):
    path = tmp_path / "db"
    schema, ids = make_legacy(path)
    db = open_database(path)
    try:
        db.begin()
        assert db.execute("DELETE FROM t WHERE id=1;")[0].affected_rows == 1
        db.rollback()
        assert db.execute("SELECT id FROM t;")[0].rows == ((0,), (1,), (2,))
        assert db.execute("DELETE FROM t WHERE id=1;")[0].affected_rows == 1
        db.execute("INSERT INTO t(id,name) VALUES (4,'new');")
        assert db.execute("SELECT * FROM keep;")[0].rows == ((9,),)
    finally:
        db.close()
    raw = (path / "minisql.db").read_bytes()
    assert decode_header(raw[ids[1].page_id * PAGE_SIZE:]).table_id == schema.table_id
    db = open_database(path)
    try:
        assert sorted(db.execute("SELECT id FROM t;")[0].rows) == [(0,), (2,), (4,)]
        assert db.execute("DELETE FROM t;")[0].affected_rows == 3
    finally:
        db.close()


def test_legacy_cross_table_delete_rejected_including_catalog(tmp_path):
    path = tmp_path / "db"
    schema, ids = make_legacy(path)
    db = open_database(path)
    try:
        # 验证 0 不会被直接当作系统表归属或任意用户表归属。
        system = TableSchema("__catalog", (), 0)
        for target in (db.catalog.get_table("keep"), system):
            with pytest.raises(MiniSQLError, match="INVALID_RECORD"):
                db.storage.delete(target, ids[0])
        assert len(db.execute("SELECT * FROM t;")[0].rows) == 3
        assert db.execute("SELECT * FROM keep;")[0].rows == ((9,),)
    finally:
        db.close()


def test_legacy_membership_with_single_page_cache_and_cyclic_chain(tmp_path):
    path = tmp_path / "db"
    schema, ids = make_legacy(path)
    pages = DiskPageManager(FileManager(path / "minisql.db"))
    storage = HeapStorage(pages, PageBufferPool(pages, capacity=1))
    try:
        storage.delete(schema, ids[-1])
        assert [r.row[0] for r in storage.scan(schema)] == [0, 1]
        # 创建循环，并使用不在链中的旧页作为目标，必须有限时间内拒绝。
        other = storage.create_table(TableSchema("other", (ColumnSchema("id", DataType.INT),)))
        foreign = storage.insert(other, (9,))
        page = storage.buffer.get_page(foreign.page_id)
        page[:HEADER_SIZE] = encode_header(replace(decode_header(page), table_id=0))
        storage.buffer.mark_dirty(foreign.page_id)
        tail = storage.buffer.get_page(ids[-1].page_id)
        tail[:HEADER_SIZE] = encode_header(replace(decode_header(tail), next_data_page=ids[0].page_id))
        storage.buffer.mark_dirty(ids[-1].page_id)
        with pytest.raises(MiniSQLError, match="IO_ERROR"):
            storage.delete(schema, foreign)
    finally:
        storage.close()
