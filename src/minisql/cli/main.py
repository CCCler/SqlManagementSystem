"""CLI：交互模式、SQL 文件执行、结果表格输出。"""
import argparse
import math
import sys
from pathlib import Path

from minisql.compiler.statements import scan_statements
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
    interactive = sys.stdin.isatty()
    buffer = ""

    def finish() -> int:
        scanned = scan_statements(buffer)
        if not scanned.has_pending:
            return 0
        reason = {"normal": "缺少结束分号", "string": "字符串未闭合",
                  "block_comment": "块注释未闭合"}[scanned.state]
        print(f"退出：未完成的 SQL 输入未执行（{reason}）。", file=sys.stderr)
        return 1

    while True:
        pending = scan_statements(buffer)
        prompt = ("   ...> " if pending.has_pending else "minisql> ") if interactive else ""
        try:
            line = input(prompt)
        except (EOFError, OSError):
            return finish()
        except KeyboardInterrupt:
            print()
            if pending.has_pending:
                print("已取消未完成的 SQL 输入。", file=sys.stderr)
                buffer = ""
                continue
            return 0
        # 字符串/块注释中的整行 exit/quit 是 SQL 内容，不能抢先退出。
        if pending.state == "normal" and line.strip().lower() in ("exit", "quit"):
            return finish()
        buffer += line + "\n"  # 保留空行和行注释的换行边界。
        scanned = scan_statements(buffer)
        if not scanned.ends:
            if not scanned.has_pending:
                buffer = ""
            continue
        end = scanned.ends[-1]
        sql, buffer = buffer[:end], buffer[end:]
        try:
            results = database.execute(sql)
        except NotImplementedError:
            print("尚未实现：依赖的编译或存储模块尚未完成。", file=sys.stderr)
            buffer = ""
            continue
        except MiniSQLError as error:
            print(f"错误: {error}", file=sys.stderr)
            buffer = ""  # 同一批输入遇错后不继续执行其残缺尾部。
            continue
        for result in results:
            print(render_result(result))
        if not scan_statements(buffer).has_pending:
            buffer = ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MiniSQL 教学数据库")
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="数据库目录")
    parser.add_argument("--file", type=Path, help="执行 SQL 文件；省略时进入交互模式")
    parser.add_argument("--lock-timeout", type=float, help="数据库锁等待秒数，默认 5 秒")
    parser.add_argument("--version", action="version", version="minisql 0.1.0")
    args = parser.parse_args(argv)
    if args.lock_timeout is not None and (not math.isfinite(args.lock_timeout) or args.lock_timeout < 0):
        parser.error("--lock-timeout 必须是有限非负数")
    try:
        database = (open_database(args.data_dir) if args.lock_timeout is None else
                    open_database(args.data_dir, lock_timeout=args.lock_timeout))
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
            result = _run_file(database, args.file)
        else:
            result = _run_interactive(database)
        if getattr(database, "in_transaction", False):
            database.rollback()
            print("退出时存在未提交事务，已回滚。", file=sys.stderr)
            return 1
        return result
    finally:
        database.close()
