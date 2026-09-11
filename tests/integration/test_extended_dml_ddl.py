"""扩展 DML/DDL 验收：约束、表结构变更、索引维护、视图、触发器与鉴权。

覆盖 F06/F07/F09/F10/F11/F12 的真实文件执行与重开恢复。"""
import pytest

from minisql.contracts.errors import MiniSQLError
from minisql.engine.database import open_database


@pytest.fixture
def db(tmp_path):
    database = open_database(tmp_path / "db")
    yield database
    database.close()


def rows(db, sql):
    result = db.execute(sql)[0]
    return result.rows


# ---------- F07 数据约束 ----------

def test_constraints_enforced_on_write(db):
    db.execute("CREATE TABLE t(id INT PRIMARY KEY, name VARCHAR NOT NULL,"
               " score INT CHECK (score >= 0));")
    db.execute("INSERT INTO t(id,name,score) VALUES (1,'a',10);")
    with pytest.raises(MiniSQLError) as error:
        db.execute("INSERT INTO t(id,name,score) VALUES (1,'b',5);")
    assert error.value.code == "DUPLICATE_KEY"
    with pytest.raises(MiniSQLError) as error:
        db.execute("INSERT INTO t(id,name,score) VALUES (2,NULL,5);")
    assert error.value.code == "NOT_NULL_VIOLATION"
    with pytest.raises(MiniSQLError) as error:
        db.execute("INSERT INTO t(id,name,score) VALUES (3,'c',-1);")
    assert error.value.code == "CHECK_VIOLATION"
    assert rows(db, "SELECT id FROM t;") == ((1,),)


def test_unique_allows_multiple_nulls_and_check_unknown_passes(db):
    db.execute("CREATE TABLE t(id INT, code VARCHAR UNIQUE, note VARCHAR CHECK (note <> 'bad'));")
    db.execute("INSERT INTO t(id,code,note) VALUES (1,NULL,NULL);")
    db.execute("INSERT INTO t(id,code,note) VALUES (2,NULL,'ok');")  # UNIQUE 允许多个 NULL；CHECK UNKNOWN 放行
    with pytest.raises(MiniSQLError) as error:
        db.execute("INSERT INTO t(id,code,note) VALUES (3,'x','bad');")
    assert error.value.code == "CHECK_VIOLATION"
    db.execute("INSERT INTO t(id,code,note) VALUES (3,'x','fine');")
    with pytest.raises(MiniSQLError) as error:
        db.execute("INSERT INTO t(id,code,note) VALUES (4,'x','fine');")
    assert error.value.code == "DUPLICATE_KEY"


def test_foreign_key_child_and_parent_protection(db):
    db.execute("CREATE TABLE parent(id INT PRIMARY KEY, name VARCHAR);")
    db.execute("CREATE TABLE child(id INT PRIMARY KEY, pid INT REFERENCES parent(id));")
    db.execute("INSERT INTO parent(id,name) VALUES (1,'p');")
    db.execute("INSERT INTO child(id,pid) VALUES (10,1);")
    db.execute("INSERT INTO child(id,pid) VALUES (11,NULL);")  # MATCH SIMPLE：NULL 不检查
    with pytest.raises(MiniSQLError) as error:
        db.execute("INSERT INTO child(id,pid) VALUES (12,99);")
    assert error.value.code == "FOREIGN_KEY_VIOLATION"
    with pytest.raises(MiniSQLError) as error:
        db.execute("DELETE FROM parent WHERE id = 1;")
    assert error.value.code == "FOREIGN_KEY_VIOLATION"
    db.execute("DELETE FROM child WHERE id = 10;")
    db.execute("DELETE FROM parent WHERE id = 1;")
    assert rows(db, "SELECT id FROM parent;") == ()


# ---------- F06 表结构变更 ----------

def test_alter_table_add_rename_drop_and_persistence(tmp_path):
    path = tmp_path / "db"
    db = open_database(path)
    db.execute("CREATE TABLE t(id INT, name VARCHAR);")
    db.execute("INSERT INTO t(id,name) VALUES (1,'a');")
    db.execute("ALTER TABLE t ADD COLUMN score INT DEFAULT 7;")
    assert rows(db, "SELECT id,name,score FROM t;") == ((1, "a", 7),)  # 存量行补默认值
    db.execute("ALTER TABLE t RENAME COLUMN score TO points;")
    assert rows(db, "SELECT id,points FROM t;") == ((1, 7),)
    db.execute("ALTER TABLE t DROP COLUMN name;")
    assert rows(db, "SELECT id,points FROM t;") == ((1, 7),)
    db.close()
    reopened = open_database(path)
    try:
        assert reopened.execute("SELECT * FROM t;")[0].columns == ("id", "points")
        assert reopened.execute("SELECT * FROM t;")[0].rows == ((1, 7),)
    finally:
        reopened.close()


# ---------- F09 索引 ----------

def test_index_maintenance_and_query_plan(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        db.execute("CREATE TABLE t(id INT PRIMARY KEY, name VARCHAR);")
        for i, name in ((1, "a"), (2, "b"), (3, "c")):
            db.execute(f"INSERT INTO t(id,name) VALUES ({i},'{name}');")
        db.execute("CREATE INDEX by_name ON t(name);")  # 存量数据建索引
        assert rows(db, "SELECT id FROM t WHERE name = 'b';") == ((2,),)
        plan = db.execute("EXPLAIN SELECT id FROM t WHERE name = 'b';")[0].message
        assert "IndexScan" in plan  # 查询走索引路径
        db.execute("INSERT INTO t(id,name) VALUES (4,'b');")   # 重复键允许（非唯一索引）
        db.execute("UPDATE t SET name = 'z' WHERE id = 1;")
        db.execute("DELETE FROM t WHERE id = 2;")
        assert sorted(rows(db, "SELECT id FROM t WHERE name = 'b';")) == [(4,)]
        assert rows(db, "SELECT id FROM t WHERE name = 'z';") == ((1,),)
        db.close()
        reopened = open_database(tmp_path / "db")
        try:  # 重开后索引仍可用
            assert reopened.execute("SELECT id FROM t WHERE name = 'z';")[0].rows == ((1,),)
        finally:
            reopened.close()
    finally:
        db.close()


def test_unique_index_rejects_duplicates(db, tmp_path):
    try:
        db.execute("CREATE TABLE t(id INT, code VARCHAR);")
        db.execute("INSERT INTO t(id,code) VALUES (1,'x');")
        db.execute("INSERT INTO t(id,code) VALUES (2,'x');")
        with pytest.raises(MiniSQLError) as error:
            db.execute("CREATE UNIQUE INDEX uq ON t(code);")  # 存量重复在建索引时暴露
        assert error.value.code == "DUPLICATE_KEY"
        db.execute("DELETE FROM t WHERE id = 2;")
        db.execute("CREATE UNIQUE INDEX uq ON t(code);")
        db.execute("INSERT INTO t(id,code) VALUES (3,'y');")
        with pytest.raises(MiniSQLError) as error:
            db.execute("INSERT INTO t(id,code) VALUES (4,'y');")
        assert error.value.code == "DUPLICATE_KEY"
    finally:
        db.close()


# ---------- F10 视图 ----------

def test_view_lifecycle_nested_and_dependency(tmp_path):
    path = tmp_path / "db"
    db = open_database(path)
    db.execute("CREATE TABLE t(id INT, score INT);")
    db.execute("INSERT INTO t(id,score) VALUES (1,10);")
    db.execute("INSERT INTO t(id,score) VALUES (2,5);")
    db.execute("CREATE VIEW high AS SELECT id, score FROM t WHERE score > 6;")
    db.execute("CREATE VIEW top1 AS SELECT id FROM high ORDER BY id LIMIT 1;")
    assert rows(db, "SELECT id FROM top1;") == ((1,),)
    with pytest.raises(MiniSQLError) as error:
        db.execute("DROP TABLE t;")  # 依赖保护
    assert error.value.code in ("DEPENDENT_OBJECT", "READ_ONLY_VIEW", "FEATURE_NOT_EXECUTABLE")
    with pytest.raises(MiniSQLError) as error:
        db.execute("DROP VIEW high;")  # top1 依赖 high
    assert error.value.code == "DEPENDENT_OBJECT"
    db.execute("DROP VIEW top1;")
    db.execute("DROP VIEW high;")
    db.close()
    reopened = open_database(path)
    try:  # 视图重开后仍可查询
        reopened.execute("CREATE VIEW v AS SELECT id FROM t WHERE id = 2;")
        assert reopened.execute("SELECT id FROM v;")[0].rows == ((2,),)
    finally:
        reopened.close()


# ---------- F11 触发器 ----------

def test_trigger_events_and_persistence(tmp_path):
    path = tmp_path / "db"
    db = open_database(path)
    db.execute("CREATE TABLE t(id INT, name VARCHAR);")
    db.execute("CREATE TABLE audit(id INT, tag VARCHAR);")
    db.execute("CREATE TRIGGER tr_i AFTER INSERT ON t FOR EACH ROW"
               " INSERT INTO audit(id,tag) VALUES (NEW.id,'ins');")
    db.execute("CREATE TRIGGER tr_u AFTER UPDATE ON t FOR EACH ROW"
               " INSERT INTO audit(id,tag) VALUES (OLD.id,'upd');")
    db.execute("CREATE TRIGGER tr_d AFTER DELETE ON t FOR EACH ROW"
               " INSERT INTO audit(id,tag) VALUES (OLD.id,'del');")
    db.execute("INSERT INTO t(id,name) VALUES (1,'a');")
    db.execute("UPDATE t SET name='b' WHERE id=1;")
    db.execute("DELETE FROM t WHERE id=1;")
    assert rows(db, "SELECT tag FROM audit ORDER BY tag;") == (("del",), ("ins",), ("upd",))
    db.close()
    reopened = open_database(path)
    try:  # 重开后触发器继续生效
        reopened.execute("INSERT INTO t(id,name) VALUES (7,'z');")
        assert reopened.execute("SELECT id FROM audit WHERE tag='ins';")[0].rows == ((1,), (7,))
    finally:
        reopened.close()


def test_trigger_failure_rolls_back_statement(db):
    db.execute("CREATE TABLE t(id INT PRIMARY KEY);")
    db.execute("CREATE TABLE audit(id INT PRIMARY KEY);")
    db.execute("CREATE TRIGGER tr AFTER INSERT ON t FOR EACH ROW"
               " INSERT INTO audit(id) VALUES (NEW.id);")
    db.execute("INSERT INTO t(id) VALUES (1);")
    with pytest.raises(MiniSQLError) as error:
        db.execute("INSERT INTO t(id) VALUES (1);")  # 主键冲突，动作不执行
    assert error.value.code == "DUPLICATE_KEY"
    assert rows(db, "SELECT id FROM audit;") == ((1,),)


# ---------- F12 用户与权限 ----------

def test_auth_first_admin_login_and_permissions(tmp_path):
    path = tmp_path / "db"
    db = open_database(path)
    db.execute("CREATE TABLE t(id INT);")
    db.execute("INSERT INTO t(id) VALUES (1);")
    db.execute("CREATE USER admin IDENTIFIED BY 'rootpw';")   # 首个账户自动成为管理员
    assert db.accounts.accounts["admin"].is_admin
    with pytest.raises(MiniSQLError) as error:
        db.execute("SELECT * FROM t;")                        # 已有账户：未登录被拒
    assert error.value.code == "NOT_LOGGED_IN"
    assert not db.login("admin", "wrong")
    assert db.login("admin", "rootpw")
    db.execute("CREATE USER alice IDENTIFIED BY 'pw';")
    db.execute("GRANT SELECT ON TABLE t TO alice;")
    db.logout()
    assert db.login("alice", "pw")
    assert rows(db, "SELECT id FROM t;") == ((1,),)
    with pytest.raises(MiniSQLError) as error:
        db.execute("INSERT INTO t(id) VALUES (2);")           # 无 INSERT 权限
    assert error.value.code == "PERMISSION_DENIED"
    with pytest.raises(MiniSQLError) as error:
        db.execute("DROP TABLE t;")
    assert error.value.code == "PERMISSION_DENIED"
    db.logout()
    assert db.login("admin", "rootpw")
    db.execute("REVOKE SELECT ON TABLE t FROM alice;")
    db.logout()
    assert db.login("alice", "pw")
    with pytest.raises(MiniSQLError) as error:
        db.execute("SELECT * FROM t;")                        # 撤权立即生效
    assert error.value.code == "PERMISSION_DENIED"
    db.close()
    reopened = open_database(path)
    try:  # 账户与授权跨重启保留
        assert reopened.login("alice", "pw")
        with pytest.raises(MiniSQLError):
            reopened.execute("SELECT * FROM t;")
    finally:
        reopened.close()


def test_view_inherits_caller_permissions(db):
    db.execute("CREATE TABLE secret(id INT);")
    db.execute("INSERT INTO secret(id) VALUES (1);")
    db.execute("CREATE USER admin IDENTIFIED BY 'rootpw';")
    assert db.login("admin", "rootpw")
    db.execute("CREATE VIEW v AS SELECT id FROM secret;")
    db.execute("CREATE USER alice IDENTIFIED BY 'pw';")
    db.logout()
    assert db.login("alice", "pw")
    with pytest.raises(MiniSQLError) as error:
        db.execute("SELECT id FROM v;")  # 视图按调用者权限展开到 secret
    assert error.value.code == "PERMISSION_DENIED"
    db.logout()
    assert db.login("admin", "rootpw")
    db.execute("GRANT SELECT ON TABLE secret TO alice;")
    db.logout()
    assert db.login("alice", "pw")
    assert rows(db, "SELECT id FROM v;") == ((1,),)
