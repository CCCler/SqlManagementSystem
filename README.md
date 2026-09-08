# MiniSQL：大型平台软件设计实习

当前状态：**SQL 编译、页式存储、执行引擎与 CLI 均已实现，227 项测试全部通过、零跳过。**

以下命令在项目根目录执行。Python 3.11+；运行时使用标准库，pytest 用于测试。
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

## 运行说明

交互模式（默认数据目录 `data/`，输入 `exit` 或 `quit` 退出）：

```powershell
.\.venv\Scripts\python.exe -m minisql
.\.venv\Scripts\python.exe -m minisql --data-dir D:\my-db
```

执行 SQL 文件：

```powershell
.\.venv\Scripts\python.exe -m minisql --file examples\core.sql
```

端到端演示（core.sql 全流程 + 关闭重开持久化验证）：

```powershell
.\.venv\Scripts\python.exe examples\demo.py
```

查询结果按表格输出；错误输出到 stderr 并返回退出码 1。
运行数据默认放在 `data/`，虚拟环境、数据、日志和缓存均被 Git 忽略。

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
`tests/compiler`、`storage`、`engine`、`integration` 为各模块单元与验收测试，均已启用。
新增或修改功能时需同步更新对应测试；不以 skipped 数量证明功能完成。

仓库默认分支 `main`，远程为 https://github.com/CCCler/SqlManagementSystem.git 。
各成员从共同基线建立功能分支（feat/compiler、feat/storage、feat/engine），
测试与实现一起提交，经 PR 审查后合并。
运行数据默认放 `data/`，虚拟环境、数据、日志和缓存均被 Git 忽略。
