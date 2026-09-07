import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MiniSQL 教学数据库（当前为接口骨架）")
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="数据库目录")
    parser.add_argument("--file", type=Path, help="执行 SQL 文件；省略时进入交互模式")
    parser.add_argument("--version", action="version", version="minisql 0.1.0 (scaffold)")
    parser.parse_args(argv)
    print("尚未实现：SQL 执行和交互界面。请先完成各模块 TODO。", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
