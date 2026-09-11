"""系统表保护验收（成员二约定 5）：SQL 不得直接读写系统表。

保护在编译器语义层实现（与 extended_semantic 的 __ 前缀规则一致）：
系统表经 SQL 的 SELECT/INSERT/UPDATE/DELETE/DROP/CREATE 一律报
PROTECTED_TABLE 并携带位置；引擎内部（对象目录、账户存储、迁移）继续
使用真实目录不受影响。"""
import pytest

from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.engine.database import open_database


def build(tmp_path):
    database = open_database(tmp_path / "db")
    database.execute("CREATE TABLE t(id INT, name VARCHAR);")
    database.execute("INSERT INTO t(id, name) VALUES (1, 'a');")
    return database


@pytest.mark.parametrize("sql", [
    "SELECT * FROM __users;",
    "SELECT * FROM __catalog;",
    "SELECT name FROM __views;",
    "INSERT INTO __users(user_id) VALUES (9);",
    "DELETE FROM __grants;",
    "DROP TABLE __indexes;",
    "CREATE TABLE __evil(id INT);",
    "EXPLAIN SELECT * FROM __users;",
    "UPDATE __catalog SET id=1;",
])
def test_sql_cannot_touch_system_tables(sql, tmp_path):
    database = build(tmp_path)
    try:
        with pytest.raises(MiniSQLError) as error:
            database.execute(sql)
        assert error.value.code == "PROTECTED_TABLE"
        assert error.value.stage is ErrorStage.SEMANTIC
        assert error.value.position is not None  # 编译器保护保留行列位置
    finally:
        database.close()


def test_normal_tables_unaffected_and_objects_api_still_works(tmp_path):
    database = build(tmp_path)
    try:
        assert database.execute("SELECT * FROM t;")[0].rows == ((1, "a"),)
        # 引擎内部接口不受保护层影响
        from minisql.engine.objects import ViewDefinition
        from minisql.contracts.models import ColumnSchema, DataType
        database.objects.register_view(
            ViewDefinition("v1", "SELECT id FROM t;", (ColumnSchema("id", DataType.INT),)))
        assert database.objects.get_view("v1") is not None
        assert [table.name for table in database.catalog.list_tables()] == ["t"]
        assert database.catalog.get_table("__views") is not None  # 迁移工具仍可内部访问
    finally:
        database.close()
