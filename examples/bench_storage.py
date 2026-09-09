"""存储层更大规模性能基准：插入 / 扫描 / 删除及缓存统计。

在独立临时文件中创建单表并批量操作，输出记录数、页数、耗时与缓存命中统计，
作为存储报告的性能证据（不访问用户数据库）。

用法（项目根目录，虚拟环境内）：
    python examples/bench_storage.py [--count N] [--capacity C]
"""
import argparse
import json
import tempfile
import time
from pathlib import Path

from minisql.contracts.models import ColumnSchema, DataType, TableSchema
from minisql.storage.buffer import PageBufferPool
from minisql.storage.file_manager import FileManager
from minisql.storage.page import DiskPageManager
from minisql.storage.record import HeapStorage


def bench(count: int, capacity: int) -> dict:
    schema = TableSchema(
        "bench",
        (ColumnSchema("id", DataType.INT), ColumnSchema("name", DataType.VARCHAR)),
    )
    with tempfile.TemporaryDirectory(prefix="minisql-bench-") as tmp:
        path = Path(tmp) / "bench.db"
        files = FileManager(path)
        pages = DiskPageManager(files)
        buffer = PageBufferPool(pages, capacity=capacity, policy="LRU")
        heap = HeapStorage(pages, buffer)

        started = time.perf_counter()
        stored = heap.create_table(schema)
        create_ms = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        for i in range(count):
            heap.insert(stored, (i, f"name-{i}"))
        insert_ms = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        scanned = list(heap.scan(stored))
        scan_ms = (time.perf_counter() - started) * 1000

        # 删除偶数 id 记录，覆盖页内整理与槽复用路径。
        started = time.perf_counter()
        deleted = 0
        for record in scanned:
            if record.row[0] % 2 == 0:
                heap.delete(stored, record.record_id)
                deleted += 1
        delete_ms = (time.perf_counter() - started) * 1000

        heap.flush()
        file_bytes = path.stat().st_size
        stats = buffer.stats()
        remaining = sum(1 for _ in heap.scan(stored))
        heap.close()

    total_accesses = stats.hits + stats.misses
    return {
        "count": count,
        "capacity": capacity,
        "create_ms": round(create_ms, 3),
        "insert_ms": round(insert_ms, 3),
        "insert_rows_per_s": round(count / insert_ms * 1000) if insert_ms else 0,
        "scan_ms": round(scan_ms, 3),
        "delete_ms": round(delete_ms, 3),
        "deleted": deleted,
        "remaining": remaining,
        "file_bytes": file_bytes,
        "allocated_pages": file_bytes // 4096,
        "cache_hits": stats.hits,
        "cache_misses": stats.misses,
        "hit_rate": round(stats.hits / total_accesses, 4) if total_accesses else 0,
        "evictions": stats.evictions,
        "writebacks": buffer.writebacks,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MiniSQL 存储层更大规模性能基准（独立临时文件）")
    parser.add_argument("--count", type=int, default=20000, help="插入记录数")
    parser.add_argument("--capacity", type=int, default=64, help="缓存页容量")
    args = parser.parse_args(argv)
    result = bench(args.count, args.capacity)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
