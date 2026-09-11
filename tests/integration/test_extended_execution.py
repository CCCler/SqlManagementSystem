"""扩展查询算子的执行验收（阶段 A：单表表达式与查询算子）。

覆盖扩展表达式求值（三值逻辑、精确数值、运行期溢出）与
TableScan/Filter/ExpressionProject/Sort/Limit/Distinct 的真实数据库执行；
未接入的算子（JOIN/聚合等）保持 FEATURE_NOT_EXECUTABLE 屏障。"""
from decimal import Decimal

import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.engine.database import open_database


@pytest.fixture
def db(tmp_path):
    database = open_database(tmp_path / "db")
    database.execute("CREATE TABLE t(id INT, name VARCHAR);")
    for i, name in ((1, "a"), (2, "b"), (3, "c")):
        database.execute(f"INSERT INTO t(id,name) VALUES ({i},'{name}');")
    database.execute("CREATE TABLE n(id INT, name VARCHAR);")
    schema = database.catalog.get_table("n")
    database.storage.insert(schema, (2, None))   # NULL 进可空列，验证三值逻辑与排序
    database.storage.insert(schema, (None, "x"))
    database.storage.insert(schema, (1, "y"))
    database.storage.flush()  # 直插数据必须落盘：下个事务会重建页缓存
    yield database
    database.close()


def rows(db, sql):
    result = db.execute(sql)[0]
    return result.columns, result.rows


def test_expression_projection_with_alias(db):
    columns, result = rows(db, "SELECT id*2 AS d FROM t;")
    assert columns == ("d",)
    assert result == ((2,), (4,), (6,))


def test_where_between_in_and_like(db):
    assert rows(db, "SELECT id FROM t WHERE id BETWEEN 2 AND 3;")[1] == ((2,), (3,))
    assert rows(db, "SELECT id FROM t WHERE id IN (1, 3);")[1] == ((1,), (3,))
    assert rows(db, "SELECT id FROM t WHERE name LIKE 'a%';")[1] == ((1,),)
    assert rows(db, "SELECT id FROM t WHERE name LIKE '_';")[1] == ((1,), (2,), (3,))
    assert rows(db, "SELECT id FROM t WHERE name LIKE 'A%';")[1] == ()  # LIKE 区分大小写


def test_like_escape(db):
    db.execute("INSERT INTO t(id,name) VALUES (4,'x%y');")
    assert rows(db, "SELECT id FROM t WHERE name LIKE 'x!%y' ESCAPE '!';")[1] == ((4,),)
    assert rows(db, "SELECT id FROM t WHERE name LIKE 'x%';")[1] == ((4,),)


def test_null_three_valued_logic(db):
    # 比较 NULL 得 UNKNOWN：WHERE 不选中任何行
    assert rows(db, "SELECT id FROM t WHERE NULL = NULL;")[1] == ()
    assert rows(db, "SELECT id FROM n WHERE id > 1;")[1] == ((2,),)   # NULL 行不选中
    # NULL OR TRUE 得 TRUE，NULL OR FALSE 得 UNKNOWN
    assert rows(db, "SELECT id FROM t WHERE NULL OR id = 1;")[1] == ((1,),)
    # IS NULL 恒为 BOOL，可选中 NULL 行
    assert rows(db, "SELECT id FROM n WHERE name IS NULL;")[1] == ((2,),)
    assert rows(db, "SELECT id FROM n WHERE id IS NOT NULL;")[1] == ((2,), (1,))


def test_null_propagates_through_arithmetic_and_projection(db):
    columns, result = rows(db, "SELECT id + 1 AS x FROM n;")
    assert result == ((3,), (None,), (2,))          # NULL 参与算术仍为 NULL
    assert rows(db, "SELECT NULL AS v FROM t;")[1] == ((None,), (None,), (None,))


def test_runtime_overflow_and_division_by_zero(db):
    with pytest.raises(MiniSQLError) as error:
        db.execute("SELECT id + 9223372036854775807 AS x FROM t;")
    assert error.value.code == "INTEGER_OUT_OF_RANGE"
    with pytest.raises(MiniSQLError) as error:
        db.execute("SELECT id / 0 AS x FROM t;")
    assert error.value.code == "DIVISION_BY_ZERO"


def test_decimal_arithmetic_is_exact(db):
    columns, result = rows(db, "SELECT id * 1.5 AS x FROM t;")
    assert result == ((Decimal("1.50"),), (Decimal("3.00"),), (Decimal("4.50"),))


def test_sort_by_alias_expression_and_paging(db):
    assert rows(db, "SELECT id*2 AS d FROM t ORDER BY d DESC;")[1] == ((6,), (4,), (2,))
    assert rows(db, "SELECT id*2 AS d FROM t ORDER BY d DESC LIMIT 2 OFFSET 1;")[1] == ((4,), (2,))
    assert rows(db, "SELECT id, name FROM t ORDER BY id DESC LIMIT 1;")[1] == ((3, "c"),)


def test_sort_null_ordering(db):
    # ASC 默认 NULL LAST、DESC 默认 NULL FIRST（编译器固定契约）
    assert rows(db, "SELECT id FROM n ORDER BY id ASC;")[1] == ((1,), (2,), (None,))
    assert rows(db, "SELECT id FROM n ORDER BY id DESC;")[1] == ((None,), (2,), (1,))


def test_distinct_on_extended_projection(db):
    db.execute("INSERT INTO t(id,name) VALUES (4,'a');")
    assert rows(db, "SELECT DISTINCT name FROM t WHERE id IN (1, 4);")[1] == (("a",),)


def test_unsupported_operators_stay_gated(db):
    """尚未接入的 DDL/DML 扩展仍被屏障拒绝，不会静默改动业务数据。"""
    from minisql.contracts.errors import ErrorStage
    for sql in ("CREATE INDEX by_id ON t(id);",
                "ALTER TABLE t ADD COLUMN x INT;",
                "CREATE VIEW v AS SELECT id FROM t;"):
        with pytest.raises(MiniSQLError) as error:
            db.execute(sql)
        assert error.value.code == "FEATURE_NOT_EXECUTABLE"
        assert error.value.stage is ErrorStage.EXECUTION
    assert rows(db, "SELECT id FROM t;")[1] == ((1,), (2,), (3,))


def test_explain_extended_plan_does_not_execute(db):
    result = db.execute("EXPLAIN SELECT id*2 FROM t;")[0]
    assert "ExtendedPlan" in result.message or "ExpressionProject" in result.message
