CREATE TABLE student(id INT, name VARCHAR, age INT);
INSERT INTO student(id,name,age) VALUES (1,'Alice',20);
INSERT INTO student(id,name,age) VALUES (2,'Bob',17);
SELECT id,name FROM student WHERE age > 18;
DELETE FROM student WHERE id = 1;
SELECT id,name FROM student;
-- 预期首次查询只有 Alice，删除后只剩 Bob；关闭重启后仍只有 Bob。
