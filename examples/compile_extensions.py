"""独立编译演示：python examples/compile_extensions.py；不打开或写数据库。"""
from minisql.compiler.compiler import SQLCompiler
from minisql.contracts.models import ColumnSchema, TableSchema, DataType
from minisql.engine.executor import render_plan

class DemoCatalog:
    def get_table(self, name):
        if name in ('student','audit'):
            return TableSchema(name,(ColumnSchema('id',DataType.INT),ColumnSchema('name',DataType.VARCHAR)),1 if name=='student' else 2)
        return None
    def list_tables(self): return tuple(self.get_table(n) for n in ('student','audit'))
    def get_view(self, name): return None
    def get_index(self, name): return None
    def list_indexes(self, table): return ()
    def get_trigger(self, name): return None
    def list_triggers(self): return ()
    def get_account(self, name): return True if name=='alice' else None
    def get_dependencies(self, kind, name): return ()
    def has_database(self, name): return name=='main'

EXAMPLES = (
 'SELECT a.id,b.name FROM student a LEFT JOIN audit b ON a.id=b.id;',
 'SELECT id,COUNT(*) FROM student GROUP BY id HAVING COUNT(*)>0;',
 "SELECT id FROM student WHERE name LIKE 'A_%' AND id BETWEEN 1 AND 10;",
 'SELECT id FROM student UNION ALL SELECT id FROM audit;',
 'SELECT id*2 AS doubled FROM student ORDER BY doubled;',
 'ALTER TABLE student ADD COLUMN score DECIMAL(10,2);',
 'CREATE TABLE grades(id INT PRIMARY KEY, score INT CHECK(score>=0));',
 "SELECT NULL,1.25,DATE '2026-09-11',TRUE FROM student;",
 'CREATE INDEX by_id ON student(id);',
 'CREATE VIEW ids AS SELECT id FROM student;',
 'CREATE TRIGGER log_insert AFTER INSERT ON student FOR EACH ROW INSERT INTO audit(id,name) VALUES(NEW.id,NEW.name);',
 'GRANT SELECT ON TABLE student TO alice;',
)

if __name__=='__main__':
    for i, sql in enumerate(EXAMPLES,1):
        result=SQLCompiler().compile(sql,DemoCatalog())
        print(f'\nF{i:02}: {sql}\n{render_plan(result.optimized_plan)}')
