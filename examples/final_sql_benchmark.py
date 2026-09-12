"""最终验收的真实 SQL 索引对比；不把存储层 lookup 当作端到端查询。"""
import argparse
import json
from pathlib import Path
import platform
import tempfile

from minisql.cli.benchmark import build_database, measure_query
from minisql.engine.database import open_database


def measure(path, size):
    database = open_database(path)
    try:
        sql = f"SELECT id,name FROM t WHERE id={size // 2};"
        plan = database.execute("EXPLAIN " + sql)[0].message
        # EXPLAIN 的独立事务结束后，BEGIN 重新建立缓冲池。
        database.begin()
        before = database.storage.buffer.stats()
        cold = measure_query(database, sql)
        warm = measure_query(database, sql)
        actual = database.execute(sql)[0].rows
        assert actual == ((size // 2, f"v{size // 2}"),)
        cold_misses = cold["buffer_misses"] - before.misses
        warm_misses = warm["buffer_misses"] - cold["buffer_misses"]
        database.rollback()
        return {
            "plan": plan, "result": actual, "scanned_rows": cold["scanned_rows"],
            "cold_user_query_misses": cold_misses, "warm_user_query_misses": warm_misses,
            "cold_execute_ms": round(cold["elapsed_execute"] * 1000, 3),
            "warm_execute_ms": round(warm["elapsed_execute"] * 1000, 3),
        }
    finally:
        database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 10000])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    (root / "data").mkdir(exist_ok=True)
    report = {"python": platform.python_version(), "platform": platform.platform(),
              "scope": "真实 SQL；显式事务内查询，排除 BEGIN/ROLLBACK；冷指用户数据页未预热，目录已加载；热为同事务第二次查询；页缺失为计数差值；耗时仅供参考。",
              "cases": []}
    with tempfile.TemporaryDirectory(prefix="final-benchmark-", dir=root / "data") as directory:
        for size in args.sizes:
            if size < 1:
                parser.error("数据规模必须为正数")
            path = Path(directory) / str(size)
            build_database(path, size)
            scan = measure(path, size)
            database = open_database(path)
            try:
                database.execute("CREATE INDEX by_id ON t(id);")
            finally:
                database.close()
            index = measure(path, size)
            assert scan["result"] == index["result"]
            assert scan["scanned_rows"] == size
            assert index["scanned_rows"] == 0
            assert "IndexScan" in index["plan"]
            report["cases"].append({"size": size, "scan": scan, "index": index})
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
