# MiniSQL：大型平台软件设计实习

当前状态：**目录与接口骨架，尚未实现 SQL 编译、存储或执行功能。**

项目根目录为 `E:\zch_Projects\sql_project`。Python 3.11+；运行时使用标准库，pytest 用于测试。
课程目标是贯通 SQL → Token → AST → 语义检查 → 计划 → 执行器 → 缓存/页 → 磁盘。
不能使用 SQLite 等现成数据库代替课程要求的实现。

## 开始使用

在项目根目录执行（Windows PowerShell，需已安装 Python 3.11+）：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m minisql --help
.\.venv\Scripts\minisql.exe --version
```

当前仅帮助和版本命令可用；执行 SQL 会显示“尚未实现”并返回退出码 2。
业务占位方法抛出 `NotImplementedError`。跳过的验收测试不是已完成能力。

## 文档与负责人

- [架构设计](docs/架构设计.md)：模块划分、调用链与持久化边界。
- [接口约定](docs/接口约定.md)：三人共同遵循的输入、输出和失败行为。
- [三人分工](docs/三人分工.md)：文件责任、阶段任务、验收和报告安排。
- [SQL 文法](docs/grammar.md)：第一版语言范围。
- 成员一：`compiler`；成员二：`storage`；成员三：`engine`、`cli`。
- `contracts` 为公共契约，修改前由三人确认。

`examples/core.sql` 是将来应跑通的演示；`examples/errors.sql` 中各语句需独立准备环境后验证。

## 测试与协作

`tests/contracts` 验证已实现的类型、测试替身和模块隔离。
`tests/compiler`、`storage`、`engine`、`integration` 记录未实现能力，暂时显式跳过。
启用每项前须将其失败占位体替换为实际操作与断言，不可只删除 skip 或改成空测试。

仓库默认分支 `main`，远程为 https://github.com/CCCler/SqlManagementSystem.git 。
当前未提交、未拉取、未推送；负责人完成首次提交和远程同步后，各成员再从共同基线建立功能分支。
运行数据默认放 `data/`，虚拟环境、数据、日志和缓存均被 Git 忽略。
