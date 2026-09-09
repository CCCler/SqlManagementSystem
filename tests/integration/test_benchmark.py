"""查询统计测量函数的确定性验收：只断言计数口径，不断言耗时。"""
from minisql.cli.benchmark import build_database, buffer_delta, load_rows, measure_executor, measure_query
from minisql.engine.database import open_database


def test_load_rows_builds_expected_size(tmp_path):
    path = tmp_path / "db"
    build_database(path, 50)
    database = open_database(path)
    try:
        assert database.execute("SELECT * FROM t;")[0].rows[-1] == (49, "v49")
    finally:
        database.close()


def test_measure_query_counts_scan_and_result_rows(tmp_path):
    path = tmp_path / "db"
    build_database(path, 100)
    database = open_database(path)
    try:
        stats = measure_query(database, "SELECT * FROM t WHERE id = 1;")
        assert stats["scanned_rows"] == 100  # 含被过滤掉的 99 行
        assert stats["result_rows"] == 1
        assert stats["buffer_hits"] >= 0
        assert stats["buffer_misses"] >= 0
        assert stats["elapsed_execute"] >= 0
    finally:
        database.close()


def test_warm_cache_in_same_transaction_hits_buffer(tmp_path):
    path = tmp_path / "db"
    build_database(path, 100)
    database = open_database(path)
    try:
        database.execute("BEGIN;")
        try:
            cold = measure_query(database, "SELECT * FROM t;")
            warm_total = measure_query(database, "SELECT * FROM t;")
        finally:
            database.rollback()
        warm = buffer_delta(warm_total, cold)
        assert cold["buffer_misses"] > 0
        assert warm["buffer_misses"] == 0  # 全部页已在同一事务的缓冲池中
        assert warm["buffer_hits"] > 0
        assert warm_total["scanned_rows"] == cold["scanned_rows"] == 100
    finally:
        database.close()


def test_measure_executor_reports_only_execution(tmp_path):
    path = tmp_path / "db"
    build_database(path, 30)
    database = open_database(path)
    try:
        stats = measure_executor(database, "SELECT * FROM t;")
        assert stats["scanned_rows"] == 30
        assert stats["elapsed_executor"] >= 0
    finally:
        database.close()
