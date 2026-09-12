"""真实 SQL 查询统计：扫描行数、页访问、耗时与峰值内存。

所有指标仅统计被测量的单条语句，计数范围：
- scanned_rows：存储 scan 接口返回的记录数（含被 Filter 过滤掉的），
  系统目录表的读取不计入。
- buffer_hits / buffer_misses：当前活跃缓冲池的累计统计。自动提交模式下每条
  语句重建缓冲池，单条语句的值即该语句的页访问量；同一显式事务内多次测量
  会累加，调用方取前后差值得到单次查询的页访问。
- elapsed_execute：Database.execute 端到端耗时（编译 + 执行 + 刷新 + 事务日志）。
- elapsed_executor：仅执行器执行优化计划的耗时，不含编译与日志。
- peak_memory_bytes（可选）：tracemalloc 峰值，覆盖执行器路径。

本模块只做测量，不包含时间断言；数字仅供规模对比与决策参考。
"""
import time
import tracemalloc

from minisql.contracts.errors import MiniSQLError
from minisql.engine.database import open_database
from minisql.storage.record import HeapStorage


def measure_query(database, sql, *, peak_memory=False):
    """在给定连接上执行一条 SQL 并返回统计字典；连接保持可用。

    事务入口会重建 HeapStorage 实例，因此 scan 计数在类级别打补丁，
    保证新实例同样被统计。
    """
    counter = {"rows": 0}
    original_scan = HeapStorage.scan

    def counting_scan(self, schema):
        if schema.table_id == 0 or schema.name.startswith("__"):
            yield from original_scan(self, schema)  # 系统目录表的读取不计入扫描行数。
            return
        for record in original_scan(self, schema):
            counter["rows"] += 1
            yield record

    HeapStorage.scan = counting_scan
    try:
        if peak_memory:
            tracemalloc.start()
        start = time.perf_counter()
        results = database.execute(sql)
        elapsed_execute = time.perf_counter() - start
        if peak_memory:
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        else:
            peak = None
        buffer_stats = database.storage.buffer.stats()
    finally:
        HeapStorage.scan = original_scan
    return {
        "scanned_rows": counter["rows"],
        "result_rows": sum(len(result.rows) for result in results),
        "buffer_hits": buffer_stats.hits,
        "buffer_misses": buffer_stats.misses,
        "elapsed_execute": elapsed_execute,
        "peak_memory_bytes": peak,
    }


def buffer_delta(after, before):
    """两次测量的缓冲池统计差值，用于同一显式事务内的冷/热对比。"""
    return {
        "buffer_hits": after["buffer_hits"] - before["buffer_hits"],
        "buffer_misses": after["buffer_misses"] - before["buffer_misses"],
    }


def measure_executor(database, sql):
    """仅测量执行器执行优化计划：不含编译、刷新与事务日志。"""
    compiled = database.compiler.compile(sql, database.catalog)
    start = time.perf_counter()
    result = database.executor.execute(compiled.optimized_plan)
    elapsed = time.perf_counter() - start
    return {
        "scanned_rows": len(result.rows),
        "elapsed_executor": elapsed,
    }


def load_rows(database, table, size):
    """在单个显式事务内逐条插入 size 行。

    每条 INSERT 单独 execute：批量多语句执行因文件级位置保留的前缀填充
    呈 O(N²) 词法成本，逐条执行在显式事务内保持线性，且整库前映像日志只付一次。
    """
    database.execute("BEGIN;")
    try:
        for i in range(size):
            database.execute(f"INSERT INTO {table}(id,name) VALUES ({i},'v{i}');")
        database.execute("COMMIT;")
    except BaseException:
        try:
            database.execute("ROLLBACK;")
        except MiniSQLError:
            pass
        raise


def build_database(path, size):
    database = open_database(path)
    database.execute("CREATE TABLE t(id INT, name VARCHAR);")
    load_rows(database, "t", size)
    database.close()
