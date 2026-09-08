"""端到端演示脚本：用真实 CLI 执行 core.sql，并演示关闭重开后的数据恢复。

用法（项目根目录，虚拟环境内）：
    python examples/demo.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_cli(data_dir: Path, sql_file: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "minisql", "--data-dir", str(data_dir), "--file", str(sql_file)],
        cwd=ROOT, check=True,
    )


def run_interactive(data_dir: Path, sql: str) -> None:
    """通过交互模式执行 SQL（管道 stdin）。"""
    subprocess.run(
        [sys.executable, "-m", "minisql", "--data-dir", str(data_dir)],
        cwd=ROOT, input=sql, text=True, check=True,
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="minisql-demo-") as tmp:
        data_dir = Path(tmp)
        print("== 1. 执行 core.sql（建表 -> 插入 -> 查询 -> 删除 -> 再查） ==", flush=True)
        run_cli(data_dir, ROOT / "examples" / "core.sql")
        print()
        print("== 2. 关闭后重新打开查询（持久化验证，预期只剩 Bob） ==", flush=True)
        run_interactive(data_dir, "SELECT id,name FROM student;\nexit\n")


if __name__ == "__main__":
    main()
