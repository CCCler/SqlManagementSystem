"""CLI：交互模式、SQL 文件执行、结果表格输出。"""
import argparse
import math
import sys
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from minisql.compiler.statements import scan_statements
from minisql.contracts.errors import MiniSQLError
from minisql.contracts.models import ExecutionResult
from minisql.engine.database import open_database
from minisql.cli.trace import TracingCompiler, emit_event, to_json_value


def format_value(value) -> str:
    """结果值的前瞻渲染：NULL/DECIMAL/日期时间/BOOL 统一显示。

    规则见 docs/NULL语义与结果展示提案-成员三.md；新类型契约生效前
    INT/VARCHAR 的现有输出不变。
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def render_result(result: ExecutionResult) -> str:
    """把执行结果格式化为表格文本；非查询结果显示提示信息。"""
    if not result.columns:
        return result.message or f"OK, {result.affected_rows} 行受影响"
    widths = [len(column) for column in result.columns]
    for row in result.rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(format_value(value)))
    lines = [" | ".join(column.ljust(widths[index]) for index, column in enumerate(result.columns))]
    lines.append("-+-".join("-" * width for width in widths))
    for row in result.rows:
        lines.append(" | ".join(format_value(value).ljust(widths[index]) for index, value in enumerate(row)))
    if not result.rows:
        lines.append("(空结果集)")
    return "\n".join(lines)


def _print_result(result, trace):
    if trace:
        emit_event("execution", result=to_json_value(result))
    else:
        print(render_result(result))


def _run_file(database, path: Path, trace: bool = False) -> int:
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
        _print_result(result, trace)
    return 0


def _run_interactive(database, trace: bool = False) -> int:
    interactive = sys.stdin.isatty() and not trace
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
            print(file=sys.stderr if trace else sys.stdout)
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
            _print_result(result, trace)
        if not scan_statements(buffer).has_pending:
            buffer = ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MiniSQL 教学数据库")
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="数据库目录")
    parser.add_argument("--file", type=Path, help="执行 SQL 文件；省略时进入交互模式")
    parser.add_argument("--trace", action="store_true", help="执行 SQL 并以 JSON Lines 输出全部编译阶段及结果")
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
        if args.trace:
            database.compiler = TracingCompiler(database.compiler)
        if args.file is not None:
            result = _run_file(database, args.file, args.trace)
        else:
            result = _run_interactive(database, args.trace)
        if getattr(database, "in_transaction", False):
            database.rollback()
            print("退出时存在未提交事务，已回滚。", file=sys.stderr)
            return 1
        return result
    finally:
        database.close()
