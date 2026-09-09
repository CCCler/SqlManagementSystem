-- 在事务中展示编译与优化，最后回滚，便于重复演示。
BEGIN;
CREATE TABLE trace_student(id INT, name VARCHAR);
INSERT INTO trace_student(name,id) VALUES ('张三',20);
SELECT name FROM trace_student WHERE 1=1 AND id>10+8;
DELETE FROM trace_student WHERE FALSE OR id=2;
ROLLBACK;
