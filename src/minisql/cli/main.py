"""CLI：交互模式、SQL 文件执行、结果表格输出。"""
import argparse
import sys
from pathlib import Path

from minisql.contracts.errors import MiniSQLError
from minisql.contracts.models import ExecutionResult
from minisql.engine.database import open_database


def render_result(result: ExecutionResult) -> str:
    """把执行结果格式化为表格文本；非查询结果显示提示信息。"""
    if not result.columns:
        return result.message or f"OK, {result.affected_rows} 行受影响"
    widths = [len(column) for column in result.columns]
    for row in result.rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(str(value)))
    lines = [" | ".join(column.ljust(widths[index]) for index, column in enumerate(result.columns))]
    lines.append("-+-".join("-" * width for width in widths))
    for row in result.rows:
        lines.append(" | ".join(str(value).ljust(widths[index]) for index, value in enumerate(row)))
    if not result.rows:
        lines.append("(空结果集)")
    return "\n".join(lines)


def _run_file(database, path: Path) -> int:
    try:
        sql = path.read_text(encoding="utf-8")
    except OSError as error:
        print(f"无法读取 SQL 文件: {error}", file=sys.stderr)
        return 1
    try:
        results = database.execute(sql)
    except NotImplementedError:
        print("尚未实现：依赖的编译或存储模块尚未完成。", file=sys.stderr)
        return 2
    except MiniSQLError as error:
        print(f"错误: {error}", file=sys.stderr)
        return 1
    for result in results:
        print(render_result(result))
    return 0


def _run_interactive(database) -> int:
    prompt = "minisql> " if sys.stdin.isatty() else ""
    buffer: list[str] = []
    while True:
        try:
            line = input(prompt)
        except (EOFError, OSError):
            break  # stdin 不可读时按 EOF 正常退出
        except KeyboardInterrupt:
            print()
            break
        if not line.strip():
            continue
        if line.strip().lower() in ("exit", "quit"):
            break
        buffer.append(line)
        # 第一版以行尾分号判断语句结束；字符串内分号跨行的极端场景待阶段 4 完善。
        if not line.rstrip().endswith(";"):
            continue
        sql = "\n".join(buffer)
        buffer.clear()
        try:
            results = database.execute(sql)
        except NotImplementedError:
            print("尚未实现：依赖的编译或存储模块尚未完成。", file=sys.stderr)
            continue
        except MiniSQLError as error:
            print(f"错误: {error}", file=sys.stderr)
            continue
        for result in results:
            print(render_result(result))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MiniSQL 教学数据库")
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="数据库目录")
    parser.add_argument("--file", type=Path, help="执行 SQL 文件；省略时进入交互模式")
    parser.add_argument("--version", action="version", version="minisql 0.1.0")
    args = parser.parse_args(argv)
    try:
        database = open_database(args.data_dir)
    except NotImplementedError:
        print("尚未实现：数据库运行依赖编译器与页存储模块，联调后开放。", file=sys.stderr)
        return 2
    except MiniSQLError as error:
        print(f"错误: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"无法打开数据库目录: {error}", file=sys.stderr)
        return 1
    try:
        if args.file is not None:
            return _run_file(database, args.file)
        return _run_interactive(database)
    finally:
        database.close()
