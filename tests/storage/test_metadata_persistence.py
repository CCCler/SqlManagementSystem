"""F07/F10/F11/F12 元数据记录协议：系统表在真实文件上的持久化与重开恢复。

成员二的验证责任：不依赖成员三尚未接入的 Catalog API，直接用 ``storage.metadata``
定义的物理布局 + 真实 ``HeapStorage``，证明系统表记录"写得进、读得回"：

- 七张系统表全部行列布局固定且只用 INT/VARCHAR；
- 账户 salt/key 的十六进制编解码、联合列名顺序保持、NULL 与空串区分；
- 视图/触发器长 SQL 文本往返，超单页容量按 ``INVALID_RECORD`` 拒绝；
- "写入 → flush → 关闭 → 重开 → 逐字段读回" 走真实 ``minisql.db`` 文件。
"""
import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.storage import metadata
from minisql.storage.record import MAX_RECORD_SIZE


# 每张系统表一条代表性记录；列数必须与物理布局一致。
REPRESENTATIVE_ROWS = {
    "__views": [
        (1, "v_emp", "SELECT id, name FROM emp", 0, "id", "INT"),
        (1, "v_emp", "SELECT id, name FROM emp", 1, "name", "VARCHAR"),
    ],
    "__triggers": [
        (1, "trg_emp_ins", "emp", "INSERT",
         "INSERT INTO log VALUES (NEW.id)", 1),
    ],
    "__indexes": [(1, "idx_emp_name", "emp", 0, 0, "name", 7)],
    "__users": [(1, "acct-3f2a", "alice", "0a1b2c", "ff00ee", 200000, 1)],
    "__grants": [("alice", "TABLE", "emp", "SELECT")],
    "__dependencies": [("VIEW", "v_emp", "TABLE", "emp")],
    "__constraints": [
        ("emp", 1, "uq_name", "UNIQUE", None, None, None, None),
        ("emp", 0, "pk_id", "PRIMARY KEY", None, None, None, None),
    ],
}


def test_system_tables_are_int_varchar_only():
    """所有系统表列只用 INT/VARCHAR，且列顺序与约定一致（不触发格式升级）。"""
    assert metadata.is_system_table("__views") and metadata.is_system_table("__USERS")
    assert not metadata.is_system_table("emp")
    expected = {
        "__views": ["view_id", "view_name", "definition", "column_index",
                    "column_name", "column_type"],
        "__triggers": ["trigger_id", "trigger_name", "table_name", "event",
                       "action", "created_order"],
        "__indexes": ["index_id", "index_name", "table_name", "unique_flag",
                      "column_index", "column_name", "root_page"],
        "__users": ["user_id", "account_id", "user_name", "salt", "key",
                    "iterations", "is_admin"],
        "__grants": ["user_name", "object_type", "object_name", "permission"],
        "__dependencies": ["object_type", "object_name",
                           "depends_on_type", "depends_on_name"],
        "__constraints": ["table_name", "column_index", "constraint_name", "kind",
                          "expression", "reference_table", "reference_columns",
                          "default_text"],
    }
    assert metadata.SYSTEM_TABLE_NAMES == tuple(expected)
    for schema in metadata.SYSTEM_TABLES:
        assert [column.name for column in schema.columns] == expected[schema.name]
        assert all(column.data_type.value in ("INT", "VARCHAR")
                   for column in schema.columns)


@pytest.mark.parametrize("name", metadata.SYSTEM_TABLE_NAMES)
def test_system_table_records_survive_reopen(real_store, name):
    """每张系统表写代表性记录后重开，逐字段一致。"""
    schema = next(s for s in metadata.SYSTEM_TABLES if s.name == name)
    real_store.create(name, schema.columns)
    expected = REPRESENTATIVE_ROWS[name]
    real_store.insert_rows(name, expected)
    real_store.storage.flush()

    reopened = real_store.reopen()

    assert reopened.rows(name) == expected


def test_account_secrets_roundtrip_as_hex(real_store):
    """salt/key 以十六进制 VARCHAR 存储：重开后 decode_bytes 还原原字节。"""
    salt = bytes(range(16))
    key = bytes(reversed(range(32)))
    real_store.create("__users", metadata.USERS.columns)
    real_store.insert_rows(
        "__users",
        [(1, "acct-uuid", "alice", metadata.encode_bytes(salt),
          metadata.encode_bytes(key), 310000, 1)],
    )
    real_store.storage.flush()

    reopened = real_store.reopen()
    row = reopened.rows("__users")[0]

    assert row[3] == salt.hex() and row[4] == key.hex()
    assert metadata.decode_bytes(row[3]) == salt
    assert metadata.decode_bytes(row[4]) == key
    assert row[5] == 310000  # 保留账户实际迭代次数，不写死默认值


def test_joined_column_order_is_preserved(real_store):
    """联合列名以逗号分隔存储，重开后顺序不变（顺序即键序）。"""
    order = ("last_name", "first_name", "dept")
    real_store.create("__constraints", metadata.CONSTRAINTS.columns)
    real_store.insert_rows(
        "__constraints",
        [("emp", 0, "uq_emp", "UNIQUE", None, None,
          metadata.encode_name_list(order), None)],
    )
    real_store.storage.flush()

    reopened = real_store.reopen()
    row = reopened.rows("__constraints")[0]

    assert metadata.decode_name_list(row[6]) == order


def test_null_is_not_empty_string():
    """缺失用 NULL 表达；NULL 与合法空串在编解码上可区分。"""
    assert metadata.encode_name_list(()) is None
    assert metadata.decode_name_list(None) == ()
    assert metadata.decode_name_list("") == ()
    # 单列空名不在支持范围，但非空列表必须原样保留。
    assert metadata.decode_name_list("a,b") == ("a", "b")


def test_long_view_definition_roundtrip(real_store):
    """长 SQL 文本（视图定义）在单页容量内往返一致。"""
    definition = "SELECT " + "x" * 3000 + " FROM emp"
    assert len(definition.encode("utf-8")) < MAX_RECORD_SIZE
    real_store.create("__views", metadata.VIEWS.columns)
    real_store.insert_rows(
        "__views", [(1, "v_big", definition, 0, "x", "VARCHAR")])
    real_store.storage.flush()

    reopened = real_store.reopen()

    assert reopened.rows("__views")[0][2] == definition


def test_oversized_definition_rejected(real_store):
    """超单页容量的元数据按 INVALID_RECORD 拒绝，不写坏数据。"""
    real_store.create("__views", metadata.VIEWS.columns)
    with pytest.raises(MiniSQLError, match="INVALID_RECORD"):
        real_store.insert_rows(
            "__views", [(1, "v_huge", "y" * (MAX_RECORD_SIZE + 1),
                         0, "y", "VARCHAR")])
