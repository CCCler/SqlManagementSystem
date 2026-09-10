"""F01–F05 存储形态验证：真实文件多表扫描、变长值与新类型往返。

使用 tests/fixtures/real_storage.py 的真实存储夹具（真实 DiskPageManager +
HeapStorage），不经过内存替身；不实现或断言任何 SQL 算子。
"""
import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.contracts.models import ColumnSchema, DataType
from minisql.engine.database import open_database
from tests.fixtures.real_storage import (
    boundary_row, full_row, max_varchar_bytes, new_type_columns, null_row, open_real_store,
)


INT_S = [ColumnSchema("id", DataType.INT), ColumnSchema("name", DataType.VARCHAR)]
PAIR_S = [ColumnSchema("k", DataType.INT), ColumnSchema("v", DataType.VARCHAR)]


# ---------- 多表共存与隔离 ----------

def test_multi_table_scans_are_independent(real_store):
    real_store.create("users", INT_S)
    real_store.create("logs", PAIR_S)
    real_store.insert_rows("users", [(1, "alice"), (2, "bob")])
    real_store.insert_rows("logs", [(10, "x"), (11, "y"), (12, "z")])
    real_store.insert_rows("users", [(3, "carol")])

    assert real_store.rows("users") == [(1, "alice"), (2, "bob"), (3, "carol")]
    assert real_store.rows("logs") == [(10, "x"), (11, "y"), (12, "z")]


def test_multi_table_survive_reopen(db_path):
    store = open_real_store(db_path)
    try:
        store.create("users", INT_S)
        store.create("logs", PAIR_S)
        store.insert_rows("users", [(1, "alice"), (2, "bob")])
        store.insert_rows("logs", [(10, "x")])
        store = store.reopen()
        assert store.rows("users") == [(1, "alice"), (2, "bob")]
        assert store.rows("logs") == [(10, "x")]
    finally:
        store.close()


def test_deleted_rows_do_not_reappear_after_reopen(db_path):
    store = open_real_store(db_path)
    try:
        store.create("users", INT_S)
        rids = store.insert_rows("users", [(1, "a"), (2, "b"), (3, "c")])
        store.storage.delete(store.schema("users"), rids[1])
        store = store.reopen()
        assert store.rows("users") == [(1, "a"), (3, "c")]
    finally:
        store.close()


def test_cross_page_multi_table_with_small_cache(db_path):
    store = open_real_store(db_path, capacity=4)
    try:
        store.create("a", [ColumnSchema("s", DataType.VARCHAR)])
        store.create("b", [ColumnSchema("s", DataType.VARCHAR)])
        rows_a = [(f"a{i:04d}" + "A" * 900,) for i in range(50)]
        rows_b = [(f"b{i:04d}" + "B" * 900,) for i in range(50)]
        store.insert_rows("a", rows_a)
        store.insert_rows("b", rows_b)
        store = store.reopen(capacity=4)
        assert store.count("a") == 50
        assert store.count("b") == 50
        assert set(store.rows("a")) == set(rows_a)
        assert set(store.rows("b")) == set(rows_b)
    finally:
        store.close()


# ---------- 变长值 ----------

def test_variable_length_values_roundtrip_and_reopen(db_path):
    sizes = [0, 1, 2, 17, 100, 999, 2500, 4000]
    rows = [(f"s{i}-" + "v" * size,) for i, size in enumerate(sizes)]
    store = open_real_store(db_path)
    try:
        store.create("t", [ColumnSchema("s", DataType.VARCHAR)])
        store.insert_rows("t", rows)
        assert store.rows("t") == rows
        store = store.reopen()
        assert store.rows("t") == rows
    finally:
        store.close()


def test_varchar_byte_length_boundary_accepted_and_rejected(real_store):
    limit = max_varchar_bytes()
    real_store.create("t", [ColumnSchema("s", DataType.VARCHAR)])
    exact = "x" * limit
    real_store.insert_rows("t", [(exact,)])
    assert real_store.rows("t") == [(exact,)]
    with pytest.raises(MiniSQLError, match="记录超出单页容量"):
        real_store.insert_rows("t", [("x" * (limit + 1),)])


def test_unicode_length_counts_utf8_bytes(real_store):
    real_store.create("t", [ColumnSchema("s", DataType.VARCHAR)])
    # 1300 个 3 字节汉字 = 3900 字节，仍可编码；字符数不被误当作字节数。
    text = "漢" * 1300
    real_store.insert_rows("t", [(text,)])
    assert real_store.rows("t") == [(text,)]


# ---------- 新类型与 NULL ----------

def test_new_types_and_null_roundtrip_with_reopen(db_path):
    rows = [full_row(), null_row(), boundary_row()]
    store = open_real_store(db_path)
    try:
        store.create("t", new_type_columns())
        store.insert_rows("t", rows)
        assert store.rows("t") == rows
        store = store.reopen()
        assert store.rows("t") == rows
    finally:
        store.close()


def test_null_only_in_some_columns_survives_reopen(db_path):
    store = open_real_store(db_path)
    try:
        store.create("t", new_type_columns())
        mixed = (None, "只有文本", None, None, None, None, None)
        store.insert_rows("t", [mixed])
        store = store.reopen()
        assert store.rows("t") == [mixed]
    finally:
        store.close()


# ---------- 引擎真实文件多表（最强证据） ----------

def test_engine_real_file_two_tables_reopen(tmp_path):
    path = tmp_path / "db"
    db = open_database(path)
    try:
        db.execute(
            "CREATE TABLE users(id INT, name VARCHAR);"
            "CREATE TABLE logs(k INT, v VARCHAR);"
            "INSERT INTO users(id, name) VALUES (1, 'alice');"
            "INSERT INTO logs(k, v) VALUES (9, 'hello');"
        )
    finally:
        db.close()

    db = open_database(path)
    try:
        assert db.execute("SELECT * FROM users;")[0].rows == ((1, "alice"),)
        assert db.execute("SELECT * FROM logs;")[0].rows == ((9, "hello"),)
        assert {s.name for s in db.catalog.list_tables()} == {"users", "logs"}
    finally:
        db.close()
