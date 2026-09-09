# MiniSQL：大型平台软件设计实习

当前状态（2026-09-09，包含 DROP TABLE、交互输入完善、删除空间回收及事务恢复）：**SQL 编译器、页式存储、执行引擎和命令行已实现，核心流程、事务、并发访问及进程崩溃恢复已通过验证。** 最近全套测试为 **367 项通过，无失败、无跳过**；报告与部分边界完善工作仍待完成。

以下命令在项目根目录执行。Python 3.11+；运行时使用标准库，pytest 用于测试。
课程目标是贯通 SQL → Token → AST → 语义检查 → 计划 → 执行器 → 缓存/页 → 磁盘。
不能使用 SQLite 等现成数据库代替课程要求的实现。

## 开始使用

首次安装时，在 Windows PowerShell 执行以下命令（需已安装 Python 3.11+）。已有 `.venv` 且已安装项目时可直接启动，无需重复创建环境：

```powershell
cd E:\zch_Projects\sql_project
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m minisql --help
.\.venv\Scripts\minisql.exe --version
```

### 交互使用

以下命令均在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m minisql --data-dir .\data\practice
```

出现 `minisql>` 后，依次输入：

```sql
CREATE TABLE student(id INT, name VARCHAR, age INT);
INSERT INTO student(id,name,age) VALUES (1,'Alice',20);
INSERT INTO student(id,name,age) VALUES (2,'Bob',17);
SELECT id,name FROM student WHERE age > 18;
DELETE FROM student WHERE id = 1;
SELECT * FROM student;
```

首次查询得到 Alice，删除后的查询只剩 Bob。输入 `exit` 或 `quit` 退出（不加分号）。
再次用相同 `--data-dir` 启动后，执行 `SELECT * FROM student;` 仍可查到 Bob，无需重复建表。
数据文件为指定目录下的 `minisql.db`，省略 `--data-dir` 时默认使用项目下的 `data/`。

交互模式只把字符串和注释之外的分号视为语句结束，支持行尾注释、跨行字符串与块注释。
续行时显示 `...>`，保留字符串内的空行；同一行的完整语句先执行，未完成尾部继续等待输入。

```sql
SELECT * FROM student; -- 分号后可以写注释
```

独立一行的 `exit`/`quit` 在字符串和块注释之外退出；若仍有未完成输入，会说明原因并返回退出码 1，不自动补分号或执行。
输入结束（EOF）采用相同行为；仅空白或已闭合注释正常退出。字符串/块注释内的 `exit`/`quit` 保留为内容。
Ctrl+C 取消当前未完成输入并返回主提示符；没有待续输入时退出。已自动提交的操作保留；显式事务在退出时回滚。
同批语句遇错停止后续执行并清空该批剩余输入，之后可继续输入新 SQL。

### 执行 SQL 文件

```powershell
.\.venv\Scripts\python.exe -m minisql --data-dir .\data\demo --file .\examples\core.sql
```

示例执行建表、插入、条件查询和删除，最终只剩 Bob。首次运行请使用新数据库目录；
再次执行同一示例会因表已存在而报错，可改用 `data/demo2` 等新目录。自定义 SQL 文件使用 UTF-8 编码。
正常文件执行返回退出码 0，SQL 错误返回 1；遇错停止后续语句，自动提交模式保留先前成功语句；显式事务遇错后必须回滚。文件结束或交互退出时仍未提交的事务自动回滚，提示原因并返回 1。

### 端到端演示

演示脚本依次执行 core.sql、关闭重开持久化验证、事务回滚、错误诊断和删表重建：

```powershell
.\.venv\Scripts\python.exe examples\demo.py
```

每步输出预期结果说明；错误诊断步骤断言预期错误码，全部步骤通过后正常退出。

### 使用事务

在上述 student 表中，可以把多条操作一起提交或撤销：

```sql
BEGIN;
DELETE FROM student WHERE id = 2;
INSERT INTO student(id,name,age) VALUES (3,'Carol',21);
ROLLBACK;
SELECT * FROM student;
```

回滚后 Bob 仍在，Carol 不存在。需要保存整组操作时，将 `ROLLBACK;` 换成 `COMMIT;`。
CREATE TABLE 和 DROP TABLE 也参与事务。显式事务中任何 SQL 出错后，先执行 `ROLLBACK;` 才能继续。

其他连接在事务结束前等待，包括查询；默认最多等待 5 秒，超时返回 `DATABASE_BUSY`，可以重试。
启动参数 `--lock-timeout 10` 将等待时间设为 10 秒，`0` 表示立即检查。不要让事务长时间等待人工输入。
数据库异常退出后，用同一目录重新启动即可自动恢复；不要手动删除目录内的 `minisql.journal` 或 `minisql.lock`。
当前日志复制完整数据库，事务开始的时间、内存和日志空间与数据库大小成正比，适合课程规模数据。

### 删除整张表

在交互模式输入（会删除表结构和全部记录）：

```sql
DROP TABLE student;
```

`DELETE FROM student;` 只删除记录、保留表；`DROP TABLE student;` 删除整张表，之后可以同名重建。
删表结果在关闭重启后保持，整表数据页会释放供后续复用；不存在的表会报错，系统表 `__catalog` 不允许删除。
当前不支持 `DROP TABLE IF EXISTS`。已有数据库若曾使用 `drop` 作为表名或列名，需要注意 DROP 现为保留关键字。

## 编译跟踪与缓存实验

```powershell
# 执行示例并输出 Token、AST、语义结果、优化前后计划（JSON Lines）
.\.venv\Scripts\python.exe -m minisql --data-dir .\data\trace-demo --file .\examples\trace.sql --trace

# 比较不同容量下的 LRU/FIFO，输出命中、淘汰、脏页写回和替换日志
.\.venv\Scripts\python.exe -m minisql.cli.cache_experiment --capacities 2 3 4 --rounds 3
```

`--trace` 会执行 SQL；示例使用事务并在最后回滚。缓存实验使用独立临时页文件，分别测量持续缓存和每轮重建缓存，不代表 SQL 事务吞吐量。
WHERE 左括号与运算符合计最多 64 个，超限返回带位置的 `EXPRESSION_TOO_COMPLEX`，避免深层表达式造成递归溢出。
完整字段、统计口径和可手算的验证序列见 [编译跟踪与缓存实验](docs/编译跟踪与缓存实验.md)。

## 已实现能力与限制

- 支持 `BEGIN`、`COMMIT`、`ROLLBACK`，默认每条 SQL 自动提交；支持多个连接和进程串行访问同一数据库。
- 支持 `CREATE TABLE`、`INSERT`、单表 `SELECT`、`DELETE`、`DROP TABLE`，以及过滤、投影、`SELECT *`、`SELECT DISTINCT` 去重和 `LIMIT` 截断。
- 表列支持有符号 64 位 `INT` 和 `VARCHAR`；`BOOL` 仅用于表达式。
- 支持加减、比较、括号及 `NOT/AND/OR`；实现常量折叠、布尔化简并保留优化前后计划。
- 支持只读 `EXPLAIN`，仅渲染优化后计划树、不扫描或修改数据；运行时加减检查有符号 64 位越界并报 `INTEGER_OUT_OF_RANGE`。
- 表名、列名和关键字大小写不敏感；字符串使用单引号，两个连续单引号表示一个单引号。
- SQL 必须以分号结束；`INSERT` 必须列出全部列，允许重排。支持 `--` 和非嵌套 `/* */` 注释。
- 使用 4KB 页、LRU/FIFO 缓存、脏页写回；表结构和记录持久化，支持正常重启及写前回滚日志恢复。
- DELETE 后自动整理页内记录并回收空间，插入优先复用空槽和空闲空间；存活记录的 RecordId 保持不变。空页留给同表复用，数据库文件不主动缩小；DROP TABLE 才释放整表页供其他表复用。
- 不支持 `UPDATE`、`JOIN`、`GROUP BY`、`NULL`、浮点数和 `VARCHAR(n)`；索引尚未实现。事务采用整库独占锁与完整文件前映像日志，不支持行锁、MVCC、嵌套事务或保存点。

待办包括报告复核、测试截图和答辩材料整理。

## 文档与负责人

- [引擎报告](docs/engine_report.md)：引擎设计与阶段性验收记录。
- [架构设计](docs/架构设计.md)：模块划分、调用链与持久化边界。
- [接口约定](docs/接口约定.md)：三人共同遵循的输入、输出和失败行为。
- [三人分工](docs/三人分工.md)：文件责任、阶段任务、验收和报告安排。
- [SQL 文法](docs/grammar.md)：当前语言范围。
- [事务与恢复](docs/事务与恢复.md)：事务操作、锁等待、恢复流程和实现边界。
- 成员一：`compiler`；成员二：`storage`；成员三：`engine`、`cli`。
- `contracts` 为公共契约，修改前由三人确认。

`examples/core.sql` 已通过真实编译器、执行引擎和页存储联调；`examples/errors.sql` 中各语句需独立准备环境后验证。

## 测试与协作

`tests/contracts` 验证类型、入口及模块隔离；`tests/compiler`、`storage`、`engine` 验证模块行为。
`tests/integration` 同时包含替身隔离测试和真实数据库测试，覆盖核心 SQL、文件级错误位置、重启恢复与字符串比较。
内存替身不能作为磁盘持久化的验收证据。当前测试通过不代表所有异常和性能边界均已覆盖。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

若测试环境无法访问系统临时目录或 pytest 缓存，可使用项目内全新临时目录：

```powershell
$testTemp = Join-Path (Get-Location) ('.test-tmp-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp $testTemp
```

该临时目录只用于测试产物，完成后可删除，不要加入提交。

仓库默认分支 `main`，远程为 https://github.com/CCCler/SqlManagementSystem.git 。
工程基线已提交并推送。三人直接在各自本地的 `main` 分支开发、提交并推送到 `origin/main`。
开始工作前先同步远程；提交后执行 `git pull --rebase origin main`，处理可能的冲突并确认测试通过，再执行 `git push origin main`。
公共接口变更先在组内沟通，避免同时修改同一文件；推送被拒绝时先同步再重试，不强制推送。
运行数据默认放 `data/`，虚拟环境、数据、日志和缓存均被 Git 忽略。
