"""扩展编译专项：真实 compile 入口，目录不发生写入。"""
from copy import deepcopy
from decimal import Decimal
import json
import pytest
from minisql.compiler.compiler import SQLCompiler
from minisql.cli.trace import to_json_value, TracingCompiler
from minisql.contracts.models import ColumnSchema as C, TableSchema as T, DataType as D
from minisql.contracts.extensions import Constraint, ExtendedPlan, IndexDefinition, ViewDefinition, TriggerDefinition
from minisql.contracts.errors import MiniSQLError
from tests.fakes.memory import ExtendedMemoryCatalog, MemoryCatalog

@pytest.fixture
def cat():
    return ExtendedMemoryCatalog((T('t',(C('id',D.INT),C('name',D.VARCHAR)),1),
        T('u',(C('id',D.INT,nullable=False),C('name',D.VARCHAR)),2,
          (Constraint('PRIMARY KEY',('id',), 'pk'),))),
        views=(ViewDefinition('v','SELECT id FROM t;'),),
        accounts=('alice',), indexes=(IndexDefinition('ix','u',('id','name')),),
        triggers=(TriggerDefinition('existing','u','DELETE'),))

def run(sql, cat):
    return SQLCompiler().compile(sql,cat)

def nodes(plan):
    yield plan
    for c in plan.children:
        yield from nodes(c)

@pytest.mark.parametrize('sql,operator',[
 ('SELECT a.id,b.name FROM t a JOIN u b ON a.id=b.id;', 'Join'),
 ('SELECT a.*,b.id FROM t a LEFT JOIN u b ON a.id=b.id;', 'Join'),
 ('SELECT a.id FROM t a RIGHT JOIN u b ON a.id=b.id CROSS JOIN t c;', 'Join'),
 ('SELECT id+1 AS next_id FROM t ORDER BY next_id;', 'Sort'),
 ('SELECT id,COUNT(*),SUM(id),AVG(id),MAX(name),MIN(id) FROM t GROUP BY id HAVING COUNT(*)>0;', 'Aggregate'),
 ("SELECT id FROM t WHERE name NOT LIKE 'a_%' ESCAPE '!' AND id NOT BETWEEN 1 AND 2;", 'Filter'),
 ('SELECT id FROM t WHERE id IN (1,2,NULL) AND id IS NOT NULL;', 'Filter'),
 ('SELECT a.id FROM t a WHERE EXISTS (SELECT b.id FROM t b WHERE b.id=a.id);', 'Filter'),
 ('SELECT (SELECT b.id FROM t b WHERE b.id=a.id) AS x FROM t a;', 'ExpressionProject'),
 ('SELECT q.id FROM (SELECT id FROM t) q;', 'DerivedTable'),
 ('SELECT id FROM t UNION ALL SELECT id FROM u INTERSECT SELECT id FROM t ORDER BY id LIMIT 4 OFFSET 1;', 'SetOperation'),
 ('(SELECT id FROM t UNION SELECT id FROM u) EXCEPT SELECT id FROM t;', 'SetOperation'),
 ("SELECT NULL,1.25*2,DATE '2024-02-29',TIME '12:13:14.123456',TIMESTAMP '2024-01-01 12:00:00',TRUE FROM t;", 'ExpressionProject'),
 ('CREATE TABLE fresh (id INT PRIMARY KEY, d DECIMAL, b BOOL NOT NULL DEFAULT TRUE, dt DATE, tm TIME, ts TIMESTAMP, CONSTRAINT ck CHECK(id>0));', 'CreateTable'),
 ('CREATE TABLE fresh (id INT REFERENCES u(id) ON DELETE RESTRICT ON UPDATE NO ACTION, name VARCHAR UNIQUE);', 'CreateTable'),
 ('ALTER TABLE t ADD COLUMN price DECIMAL(10,2) DEFAULT 1.25;', 'AlterTable'),
 ('ALTER TABLE t DROP COLUMN name;', 'AlterTable'),
 ('ALTER TABLE t RENAME COLUMN name TO label;', 'AlterTable'),
 ('ALTER TABLE t RENAME TO fresh;', 'AlterTable'),
 ('ALTER TABLE t ALTER COLUMN id TYPE DECIMAL(20,0);', 'AlterTable'),
 ('ALTER TABLE t ADD CONSTRAINT uq UNIQUE(id);', 'AlterTable'),
 ('ALTER TABLE u DROP CONSTRAINT pk;', 'AlterTable'),
 ('CREATE UNIQUE INDEX fresh ON t(id,name);', 'CreateIndex'),
 ('DROP INDEX ix;', 'DropIndex'),
 ('CREATE VIEW fresh (key_id) AS SELECT id FROM t;', 'CreateView'),
 ('SELECT v.id FROM v;', 'ViewScan'),
 ('DROP VIEW v;', 'DropView'),
 ('CREATE TRIGGER tr AFTER INSERT ON t FOR EACH ROW INSERT INTO u(id,name) VALUES (NEW.id,NEW.name);', 'CreateTrigger'),
 ('CREATE TRIGGER tr AFTER UPDATE ON t FOR EACH ROW BEGIN UPDATE u SET name=NEW.name WHERE id=OLD.id; SELECT NEW.id FROM u; END;', 'CreateTrigger'),
 ('CREATE TRIGGER tr AFTER DELETE ON t FOR EACH ROW DELETE FROM u WHERE id=OLD.id;', 'CreateTrigger'),
 ('DROP TRIGGER existing;', 'DropTrigger'),
 ("CREATE USER bob IDENTIFIED BY 'sentinel';", 'CreateUser'),
 ("ALTER USER alice IDENTIFIED BY 'sentinel';", 'AlterUser'),
 ('DROP USER alice;', 'DropUser'),
 ('GRANT SELECT,UPDATE ON TABLE t TO alice;', 'Grant'),
 ('REVOKE SELECT ON VIEW v FROM alice;', 'Revoke'),
 ('GRANT CREATE TABLE,CREATE INDEX ON DATABASE main TO alice;', 'Grant'),
 ('SELECT * FROM t WHERE id=1.2;', 'Filter'),
 ('SELECT * FROM t WHERE id*2=4;', 'Filter'),
 ('SELECT * FROM t WHERE id=NULL;', 'Filter'),
 ('CREATE TABLE fresh (a BOOL);', 'CreateTable'),
])
def test_legal(sql,operator,cat):
    before=to_json_value(cat.tables)
    result=run(sql,cat)
    assert operator in {n.operator for n in nodes(result.plan)}
    assert result.required_capabilities
    assert to_json_value(cat.tables)==before
    assert result.optimized_plan is not result.plan
    json.dumps(to_json_value(result))

@pytest.mark.parametrize('sql,code',[
 ('SELECT id FROM t a JOIN t b ON a.id=b.id;','AMBIGUOUS_COLUMN'),
 ('SELECT a.id FROM t a JOIN t a ON a.id=a.id;','DUPLICATE_ALIAS'),
 ('SELECT a.bad FROM t a;','UNKNOWN_COLUMN'),
 ('SELECT a.id FROM t a JOIN t b;','UNEXPECTED_TOKEN'),
 ('SELECT a.id FROM t a CROSS JOIN t b ON a.id=b.id;','UNEXPECTED_TOKEN'),
 ('SELECT id,COUNT(*) FROM t;','NON_GROUPED_COLUMN'),
 ('SELECT SUM(COUNT(id)) FROM t;','INVALID_AGGREGATE'),
 ('SELECT id FROM t WHERE COUNT(*)>0;','INVALID_AGGREGATE'),
 ('SELECT SUM(name) FROM t;','TYPE_MISMATCH'),
 ('SELECT id FROM t HAVING id>1;','NON_GROUPED_COLUMN'),
 ('SELECT id FROM t WHERE id IN ();','UNEXPECTED_TOKEN'),
 ("SELECT id FROM t WHERE name LIKE 'x' ESCAPE 'xx';",'INVALID_ESCAPE'),
 ("SELECT id FROM t WHERE id LIKE 'x';",'TYPE_MISMATCH'),
 ('SELECT (SELECT id,name FROM t) FROM t;','SUBQUERY_COLUMN_COUNT'),
 ('SELECT q.id FROM (SELECT id FROM t);','UNEXPECTED_TOKEN'),
 ('SELECT q.id FROM t a CROSS JOIN (SELECT a.id FROM u) q;','UNKNOWN_COLUMN'),
 ('SELECT id FROM t UNION SELECT id,name FROM u;','SET_COLUMN_COUNT'),
 ('SELECT id FROM t UNION SELECT name FROM t;','TYPE_MISMATCH'),
 ('SELECT id FROM t INTERSECT ALL SELECT id FROM u;','UNEXPECTED_TOKEN'),
 ('SELECT DISTINCT id+1 AS n FROM t ORDER BY name;','INVALID_ORDER_BY'),
 ('SELECT id+TRUE FROM t;','TYPE_MISMATCH'),
 ("SELECT DATE '2023-02-29' FROM t;",'INVALID_LITERAL'),
 ("SELECT TIME '12:00:00.1234567' FROM t;",'INVALID_LITERAL'),
 ('CREATE TABLE fresh (d DECIMAL(39,2));','INVALID_TYPE'),
 ('CREATE TABLE fresh (d DECIMAL(2,3));','INVALID_TYPE'),
 ('CREATE TABLE fresh (d DECIMAL(3,2) DEFAULT 10.00);','NUMERIC_OUT_OF_RANGE'),
 ('CREATE TABLE fresh (d DECIMAL(3,2) DEFAULT 1.001);','NUMERIC_OUT_OF_RANGE'),
 ('CREATE TABLE fresh (id INT DEFAULT id);','UNKNOWN_COLUMN'),
 ('CREATE TABLE fresh (id INT CHECK(SUM(id)>0));','INVALID_AGGREGATE'),
 ('CREATE TABLE fresh (id INT CHECK(EXISTS(SELECT id FROM t)));','INVALID_SUBQUERY'),
 ('CREATE TABLE fresh (id INT REFERENCES t(id));','INVALID_FOREIGN_KEY'),
 ('CREATE TABLE fresh (id VARCHAR REFERENCES u(id));','TYPE_MISMATCH'),
 ('ALTER TABLE t DROP COLUMN bad;','UNKNOWN_COLUMN'),
 ('ALTER TABLE t ADD COLUMN id INT;','DUPLICATE_COLUMN'),
 ('CREATE INDEX fresh ON t(id,id);','DUPLICATE_COLUMN'),
 ('CREATE INDEX fresh ON t(bad);','UNKNOWN_COLUMN'),
 ('CREATE INDEX ix ON t(id);','DUPLICATE_INDEX'),
 ('CREATE VIEW fresh AS SELECT id,id FROM t;','DUPLICATE_COLUMN'),
 ('CREATE VIEW fresh AS SELECT * FROM fresh;','CYCLIC_VIEW'),
 ('UPDATE v SET id=1;','READ_ONLY_VIEW'),
 ('CREATE TRIGGER tr AFTER INSERT ON t FOR EACH ROW SELECT OLD.id FROM u;','UNKNOWN_COLUMN'),
 ('CREATE TRIGGER tr AFTER INSERT ON t FOR EACH ROW INSERT INTO t(id,name) VALUES(NEW.id,NEW.name);','RECURSIVE_TRIGGER'),
 ('CREATE TRIGGER tr AFTER INSERT ON t FOR EACH ROW BEGIN DROP TABLE u; END;','UNEXPECTED_TOKEN'),
 ("CREATE USER alice IDENTIFIED BY 'x';",'DUPLICATE_USER'),
 ("ALTER USER absent IDENTIFIED BY 'x';",'UNKNOWN_USER'),
 ('GRANT INSERT ON VIEW v TO alice;','UNKNOWN_PERMISSION'),
 ('GRANT SELECT ON TABLE t TO absent;','UNKNOWN_USER'),
 ('GRANT SELECT ON DATABASE main TO alice;','UNKNOWN_PERMISSION'),
])
def test_invalid(sql,code,cat):
    with pytest.raises(MiniSQLError) as error: run(sql,cat)
    assert error.value.code==code
    assert error.value.position is not None

def test_binding_outer_null_and_self_join(cat):
    p=run('SELECT a.id,b.id FROM t a LEFT JOIN u b ON a.id=b.id;',cat).plan
    a,b=p.expressions
    assert a.binding.source != b.binding.source
    assert b.type.nullable

def test_index_prefix_and_original_immutable(cat):
    result=run("SELECT id FROM u WHERE id=1 AND name>'a';",cat)
    assert 'IndexScan' not in {n.operator for n in nodes(result.plan)}
    scan=next(n for n in nodes(result.optimized_plan) if n.operator=='IndexScan')
    assert dict(scan.attributes)['index']=='ix'
    assert len(dict(scan.attributes)['bounds'])==2
    assert 'index_scan' in result.required_capabilities
    for sql in ("SELECT id FROM u WHERE name='a';", "SELECT id FROM u WHERE id=1 OR id=2;",
                "SELECT a.id FROM t a LEFT JOIN u b ON a.id=b.id WHERE b.id=1;"):
        assert 'IndexScan' not in {n.operator for n in nodes(run(sql,cat).optimized_plan)}

def test_view_cycles(cat):
    cat.views['v']=ViewDefinition('v','SELECT * FROM w;')
    cat.views['w']=ViewDefinition('w','SELECT * FROM v;')
    with pytest.raises(MiniSQLError,match='CYCLIC_VIEW'): run('SELECT * FROM v;',cat)

def test_missing_catalog():
    with pytest.raises(MiniSQLError,match='CATALOG_CAPABILITY_UNAVAILABLE'):
        run("CREATE USER bob IDENTIFIED BY 'x';",MemoryCatalog())

def test_trigger_split(cat):
    sql="CREATE TRIGGER tr AFTER UPDATE ON t FOR EACH ROW BEGIN SELECT 'x;y' FROM u; /* ; */ SELECT NEW.id FROM u; END; BEGIN; ROLLBACK;"
    parts=SQLCompiler().split_statements(sql)
    assert len(parts)==3
    assert run(parts[0],cat).plan.operator=='CreateTrigger'

def test_password(cat,capsys):
    secret='UNIQUE_PASSWORD_SENTINEL'
    result=TracingCompiler(SQLCompiler()).compile(f"CREATE USER bob IDENTIFIED BY '{secret}';",cat)
    assert result.plan.password==secret
    assert secret not in repr(result)
    assert secret not in json.dumps(to_json_value(result))
    assert secret not in capsys.readouterr().out
    with pytest.raises(MiniSQLError) as error:
        run(f"CREATE USER bob IDENTIFIED BY '{secret}' '{secret}';",cat)
    assert secret not in str(error.value)

@pytest.mark.parametrize('expr',['1/0','9223372036854775807+1'])
def test_dangerous_fold_preserved(expr,cat):
    result=run(f'SELECT {expr} FROM t;',cat)
    assert result.optimized_plan.expressions[0].op != 'literal'

def test_decimal_and_null_fold(cat):
    result=run('SELECT 1/3,NULL=1,NULL IS NULL FROM t;',cat)
    a,b,c=result.optimized_plan.expressions
    assert a.args[0]==Decimal('0.3333333333333333333')
    assert b.args==(None,)
    assert c.args==(True,)

@pytest.mark.parametrize('sql',[
    'SELECT '+ '(' * 100 + 'id' + ')' * 100 + ' FROM t;',
    'SELECT '+ 'NOT '*100 + 'TRUE FROM t;',
    'SELECT '+','.join('id+1' for _ in range(1500))+' FROM t;',
])
def test_complexity(sql,cat):
    with pytest.raises(MiniSQLError) as error: run(sql,cat)
    assert error.value.code in ('EXPRESSION_TOO_COMPLEX','STATEMENT_TOO_COMPLEX')


def test_query_depth(cat):
    sql='SELECT id FROM t'
    for _ in range(17): sql='SELECT id FROM t WHERE EXISTS ('+sql+')'
    with pytest.raises(MiniSQLError,match='QUERY_TOO_DEEP'): run(sql+';',cat)

def test_shadowing_and_correlated_capability(cat):
    result=run('SELECT a.id FROM t a WHERE EXISTS(SELECT a.id FROM u a WHERE a.id=1);',cat)
    assert 'subquery' in result.required_capabilities
    outer=result.plan.expressions[0].binding
    inner=result.plan.children[0].expressions[0].args[0].expressions[0].binding
    assert outer.scope != inner.scope

def test_index_error_and_dependency(cat):
    result=run('SELECT id FROM u WHERE id=1 AND id+9223372036854775807>0;',cat)
    assert all(n.operator!='IndexScan' for n in nodes(result.optimized_plan))
    result=run('SELECT id FROM u WHERE id=1;',cat)
    assert any(d.kind=='index' and d.name=='ix' for d in result.dependencies)

def test_tie_and_reversed_range(cat):
    cat.indexes['aa']=IndexDefinition('aa','u',('id','name'))
    result=run("SELECT id FROM u WHERE 1=id AND 'a'<name;",cat)
    scan=next(n for n in nodes(result.optimized_plan) if n.operator=='IndexScan')
    assert dict(scan.attributes)['index']=='aa'
    assert dict(scan.attributes)['bounds'][1][1]=='>'

def test_assign_decimal(cat):
    cat.tables['money']=T('money',(C('d',D.DECIMAL,3,2),),9)
    assert run('INSERT INTO money(d) VALUES(1.20);',cat).plan.operator=='Insert'
    for value in ('1.201','10.0'):
        with pytest.raises(MiniSQLError,match='NUMERIC_OUT_OF_RANGE'):
            run(f'INSERT INTO money(d) VALUES({value});',cat)

# 独立受限参考求值器，仅用于常量优化等价性，不作为数据库运行时。
def reference(expr):
    import operator
    if expr.op=='literal': return expr.args[0]
    values=[reference(a) for a in expr.args]
    if expr.op=='NOT': return None if values[0] is None else not values[0]
    if expr.op in ('AND','OR'):
        a,b=values
        if expr.op=='AND': return False if False in (a,b) else None if None in (a,b) else True
        return True if True in (a,b) else None if None in (a,b) else False
    if expr.op=='IS NULL': return values[0] is None
    if None in values: return None
    return {'+':operator.add,'-':operator.sub,'*':operator.mul,'=':operator.eq,'<':operator.lt}[expr.op](*values)

@pytest.mark.parametrize('expr',[
    f'{a} {op} {b}' for a in ('TRUE','FALSE','NULL') for b in ('TRUE','FALSE','NULL') for op in ('AND','OR')
]+[f'({a}+2)*3<{b}' for a in range(-3,4) for b in (0,10)]+['NULL=1','NULL IS NULL','NOT NULL'])
def test_reference_equivalence(expr,cat):
    result=run(f'SELECT {expr} FROM t;',cat)
    assert reference(result.plan.expressions[0])==reference(result.optimized_plan.expressions[0])


def test_having_order_aggregate_contract(cat):
    result=run('SELECT 1 AS x FROM t HAVING SUM(id)>0 ORDER BY AVG(id);',cat)
    aggregate=next(n for n in nodes(result.plan) if n.operator=='Aggregate')
    assert {e.op for e in dict(aggregate.attributes)['aggregates']}=={'SUM','AVG'}

@pytest.mark.parametrize('sql,code',[
 ('SELECT COUNT() FROM t;','UNEXPECTED_TOKEN'),
 ('SELECT COUNT(missing) FROM t;','UNKNOWN_COLUMN'),
 ('SELECT id FROM t WHERE missing BETWEEN 1 AND 2;','UNKNOWN_COLUMN'),
 ("SELECT id FROM t WHERE id IN ('a');",'TYPE_MISMATCH'),
 ('SELECT missing FROM t UNION SELECT id FROM t;','UNKNOWN_COLUMN'),
 ('SELECT a.id FROM missing a;','UNKNOWN_TABLE'),
 ('SELECT id AS FROM t;','UNEXPECTED_TOKEN'),
 ('ALTER TABLE t ADD bad INT;','UNEXPECTED_TOKEN'),
 ("ALTER TABLE t ADD COLUMN x INT DEFAULT 'a';",'TYPE_MISMATCH'),
 ('CREATE TABLE fresh(id INT,PRIMARY KEY(missing));','UNKNOWN_COLUMN'),
 ('CREATE TABLE fresh(id INT,FOREIGN KEY(id) REFERENCES u(id) ON DELETE CASCADE);','UNEXPECTED_TOKEN'),
 ('CREATE INDEX fresh ON t();','UNEXPECTED_TOKEN'),
 ('CREATE VIEW fresh AS SELECT missing FROM t;','UNKNOWN_COLUMN'),
 ("CREATE VIEW fresh AS SELECT id+'x' FROM t;",'TYPE_MISMATCH'),
 ('CREATE VIEW fresh SELECT id FROM t;','UNEXPECTED_TOKEN'),
 ("CREATE TRIGGER tr AFTER INSERT ON t FOR EACH ROW UPDATE u SET id=NEW.name;",'TYPE_MISMATCH'),
 ("CREATE USER bob 'x';",'UNEXPECTED_TOKEN'),
 ('GRANT SELECT ON TABLE absent TO alice;','UNKNOWN_TABLE'),
 ('REVOKE SELECT ON TABLE t TO alice;','UNEXPECTED_TOKEN'),
])
def test_additional_category_errors(sql,code,cat):
    with pytest.raises(MiniSQLError) as error: run(sql,cat)
    assert error.value.code==code

def test_expansion_total_budget(cat):
    # 每个定义均很小，但重复展开合计过大，也必须受预算限制。
    cat.views['v0']=ViewDefinition('v0','SELECT id FROM t;')
    for i in range(1,13):
        cat.views[f'v{i}']=ViewDefinition(f'v{i}',f'SELECT a.id FROM v{i-1} a CROSS JOIN v{i-1} b;')
    with pytest.raises(MiniSQLError,match='STATEMENT_TOO_COMPLEX'):
        run('SELECT * FROM v12;',cat)


@pytest.mark.parametrize('suffix,rounded',[('0000005','000000'),('0000015','000002')])
def test_decimal_half_even_at_six_places(suffix,rounded,cat):
    integer='1234567890123456789012345678901'
    result=run(f'SELECT {integer}.{suffix}/1.0 FROM t;',cat)
    expr=result.optimized_plan.expressions[0]
    assert expr.type.precision==38 and expr.type.scale==6
    assert expr.args==(Decimal(integer+'.'+rounded),)


def test_decimal_catalog_default_parameters(cat):
    cat.tables['money']=T('money',(C('d',D.DECIMAL),),9)
    result=run('SELECT d+1 FROM money;',cat)
    assert result.output_fields[0].type.kind=='DECIMAL'
    assert result.plan.expressions[0].args[0].type.precision==18
    assert result.plan.expressions[0].args[0].type.scale==2


@pytest.mark.parametrize('sql',[
 'EXPLAIN CREATE INDEX fresh ON t(id);',
 "EXPLAIN CREATE USER bob IDENTIFIED BY 'SECRET_FOR_EXPLAIN';",
 'EXPLAIN ALTER TABLE t ADD COLUMN b BOOL;',
])
def test_explain_management(sql,cat):
    result=run(sql,cat)
    assert result.plan.operator=='Explain'
    from minisql.engine.executor import render_plan
    assert 'SECRET_FOR_EXPLAIN' not in render_plan(result.plan)
    assert 'SECRET_FOR_EXPLAIN' not in repr(result)


def test_explain_recursion_guard(cat):
    with pytest.raises(MiniSQLError,match='UNEXPECTED_TOKEN'):
        run('EXPLAIN '*1200+'SELECT id FROM t;',cat)
