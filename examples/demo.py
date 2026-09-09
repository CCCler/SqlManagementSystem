"""端到端演示脚本：真实 CLI 依次演示核心流程、重启持久化、事务回滚、错误诊断与删表重建。

用法（项目根目录，虚拟环境内）：
    python examples/demo.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_file(data_dir: Path, sql_file: Path, *, expect_error: str | None = None) -> None:
    """文件模式执行 SQL 文件。

    expect_error 提供时断言退出码 1 且 stderr 包含指定错误码，用于错误诊断演示。
    """
    completed = subprocess.run(
        [sys.executable, "-m", "minisql", "--data-dir", str(data_dir), "--file", str(sql_file)],
        cwd=ROOT, text=True, encoding="utf-8", capture_output=True,
    )
    print(completed.stdout, end="")
    if completed.stderr:
        print(f"[stderr] {completed.stderr}", end="")
    if expect_error is None:
        completed.check_returncode()
    else:
        assert completed.returncode == 1, f"预期报错退出码 1，实际 {completed.returncode}"
        assert expect_error in completed.stderr, f"stderr 未包含 {expect_error}"
        print(f"（预期错误 {expect_error} 诊断正确）")


def run_interactive(data_dir: Path, sql: str) -> None:
    """交互模式执行 SQL（管道 stdin）。"""
    completed = subprocess.run(
        [sys.executable, "-m", "minisql", "--data-dir", str(data_dir)],
        cwd=ROOT, input=sql, text=True, encoding="utf-8", capture_output=True,
    )
    print(completed.stdout, end="")
    if completed.stderr:
        print(f"[stderr] {completed.stderr}", end="")
    completed.check_returncode()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="minisql-demo-") as tmp:
        data_dir = Path(tmp)
        print("== 1. 执行 core.sql（建表 -> 插入 -> 查询 -> 删除 -> 再查） ==", flush=True)
        run_file(data_dir, ROOT / "examples" / "core.sql")
        print()
        print("== 2. 关闭后重新打开查询（持久化验证，预期只剩 Bob） ==", flush=True)
        run_interactive(data_dir, "SELECT id,name FROM student;\nexit\n")
        print()
        print("== 3. 事务回滚演示（插入 Carol 后回滚，预期仍只剩 Bob） ==", flush=True)
        run_interactive(
            data_dir,
            "BEGIN;\n"
            "INSERT INTO student(id,name,age) VALUES (3,'Carol',21);\n"
            "ROLLBACK;\n"
            "SELECT id,name FROM student;\n"
            "exit\n",
        )
        print()
        print("== 4. 错误诊断演示（查询不存在的列，预期 UNKNOWN_COLUMN） ==", flush=True)
        error_sql = Path(tmp) / "error.sql"
        error_sql.write_text("SELECT missing FROM student;", encoding="utf-8")
        run_file(data_dir, error_sql, expect_error="UNKNOWN_COLUMN")
        print()
        print("== 5. 删表与同名重建（重启后保持，预期新表只有 Alice） ==", flush=True)
        run_interactive(
            data_dir,
            "DROP TABLE student;\n"
            "CREATE TABLE student(id INT, name VARCHAR);\n"
            "INSERT INTO student(id,name) VALUES (1,'Alice');\n"
            "exit\n",
        )
        run_interactive(data_dir, "SELECT id,name FROM student;\nexit\n")


if __name__ == "__main__":
    main()
