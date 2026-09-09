"""真实 SQL 查询统计与规模验证。

在隔离数据目录的真实页存储上，比较不同数据规模、过滤条件、冷/热缓存与
事务边界的扫描行数、页访问、耗时与峰值内存。结果用于判断是否需要把扫描、
过滤、投影改为迭代处理；本脚本只报告数字，不做性能断言。

用法（项目根目录，虚拟环境内）：
    python examples/benchmark.py [--sizes 100 1000 10000] [--data-dir DIR]

耗时数字受本机负载影响，仅供数量级对比；权威证据以扫描行数与页访问为准。
"""
import argparse
import tempfile
import time
from pathlib import Path

from minisql.cli.benchmark import build_database, buffer_delta, measure_executor, measure_query
from minisql.engine.database import open_database


def row(sizes, *cells):
    return " | ".join(str(cell).ljust(sizes[i]) for i, cell in enumerate(cells))


def print_table(title, columns, lines):
    print(f"== {title} ==")
    print(row([len(c) for c in columns], *columns))
    print("-+-".join("-" * len(c) for c in columns))
    for line in lines:
        print(row([len(c) for c in columns], *line))
    print()


def measure_warm_pair(database, sql):
    """同一显式事务内连测两次：首次冷缓存（新缓冲池），第二次热缓存。

    返回 (cold, warm_delta)：cold 为首次查询的完整统计，warm_delta 为第二次
    查询的页访问差值（同一事务内缓冲池累计，取差得到热查询自身的命中/缺失）。
    """
    database.execute("BEGIN;")
    try:
        cold = measure_query(database, sql)
        warm_total = measure_query(database, sql)
    finally:
        database.rollback()
    return cold, {
        "scanned_rows": warm_total["scanned_rows"],
        "result_rows": warm_total["result_rows"],
        "elapsed_execute": warm_total["elapsed_execute"],
        **buffer_delta(warm_total, cold),
    }


def measure(database, sql):
    cold, warm_delta = measure_warm_pair(database, sql)
    executor = measure_executor(database, sql)
    lines = []
    for label, stats in (("冷", cold), ("热", warm_delta)):
        lines.append((
            sql, label,
            stats["scanned_rows"], stats["result_rows"],
            stats["buffer_hits"], stats["buffer_misses"],
            f"{stats['elapsed_execute'] * 1000:.1f}",
            f"{executor['elapsed_executor'] * 1000:.2f}",
        ))
    return lines


def run_size(data_root, size, peak_memory):
    data_dir = data_root / f"size-{size}"
    print(f"## 数据规模 {size} 行（目录 {data_dir}）")
    build_database(data_dir, size)

    queries = (
        "SELECT * FROM t;",
        "SELECT * FROM t WHERE id = 1;",
        f"SELECT * FROM t WHERE id < {size // 10};",
    )
    columns = ("查询", "缓存", "扫描行", "结果行", "命中", "缺失", "执行ms", "执行器ms")
    lines = []
    database = open_database(data_dir)
    try:
        for sql in queries:
            lines.extend(measure(database, sql))
    finally:
        database.close()
    print_table(f"规模 {size}：过滤条件与冷/热缓存（同一显式事务内）", columns, lines)

    # 事务边界：自动提交（每条 SELECT 各自承担整库前映像日志）与显式事务对比。
    database = open_database(data_dir)
    try:
        auto = measure_query(database, "SELECT * FROM t;")
        explicit_start = time.perf_counter()
        database.execute("BEGIN; SELECT * FROM t; ROLLBACK;")
        explicit_elapsed = time.perf_counter() - explicit_start
    finally:
        database.close()
    print_table(f"规模 {size}：事务边界", ("方式", "端到端ms", "扫描行"),
                [("自动提交 SELECT", f"{auto['elapsed_execute'] * 1000:.1f}", auto["scanned_rows"]),
                 ("BEGIN/ROLLBACK 内 SELECT", f"{explicit_elapsed * 1000:.1f}", auto["scanned_rows"])])

    if peak_memory:
        database = open_database(data_dir)
        try:
            stats = measure_query(database, "SELECT * FROM t;", peak_memory=True)
        finally:
            database.close()
        print(f"峰值内存（tracemalloc，SELECT *）：{stats['peak_memory_bytes'] / 1024:.1f} KiB")
    print()


def main():
    parser = argparse.ArgumentParser(description="真实 SQL 查询统计与规模验证")
    parser.add_argument("--sizes", type=int, nargs="+", default=(100, 1000, 10000))
    parser.add_argument("--data-dir", type=Path, default=None, help="数据根目录；默认使用临时目录")
    parser.add_argument("--peak-memory", action="store_true", help="使用 tracemalloc 测量峰值内存")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="minisql-benchmark-") as tmp:
        data_root = args.data_dir if args.data_dir is not None else Path(tmp)
        data_root.mkdir(parents=True, exist_ok=True)
        for size in args.sizes:
            run_size(data_root, size, args.peak_memory)


if __name__ == "__main__":
    main()
