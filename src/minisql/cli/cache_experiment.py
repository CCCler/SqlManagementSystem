"""在独立临时页文件中重放访问序列，对比缓存策略与缓存生命周期。"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory

from minisql.contracts.errors import MiniSQLError
from minisql.storage.buffer import PageBufferPool
from minisql.storage.file_manager import FileManager
from minisql.storage.page import DiskPageManager, HEADER_SIZE, PAGE_SIZE


COUNTER = struct.Struct(">Q")


def _run_case(path, accesses, write_pages, capacity, policy, rounds, scope):
    files = FileManager(path)
    expected = {page: 0 for page in set(accesses)}
    samples = []
    events = []
    try:
        pages = DiskPageManager(files)
        # 页标签映射到紧凑物理页，避免大标签创建巨大稀疏文件。
        physical = {}
        for label in sorted(expected):
            physical[label] = pages.allocate_page()
            pages.write_page(physical[label], bytes(PAGE_SIZE))
        labels = {page: label for label, page in physical.items()}
        pool = None
        for round_index in range(1, rounds + 1):
            if pool is None or scope == "reset-per-round":
                pool = PageBufferPool(pages, capacity=capacity, policy=policy)
            before = asdict(pool.stats())
            before_writes = pool.writebacks
            before_events = len(pool.replacement_log())
            for label in accesses:
                data = pool.get_page(physical[label])
                if COUNTER.unpack_from(data, HEADER_SIZE)[0] != expected[label]:
                    raise RuntimeError("缓存实验数据校验失败")
                if label in write_pages:
                    expected[label] += 1
                    COUNTER.pack_into(data, HEADER_SIZE, expected[label])
                    pool.mark_dirty(physical[label])
            # 每轮均刷新，唯一区别是下一轮是否保留缓存；统计包含本次刷新。
            pool.flush_all()
            files.sync()
            sample = {key: value - before[key] for key, value in asdict(pool.stats()).items()}
            sample.update(round=round_index, writebacks=pool.writebacks - before_writes)
            samples.append(sample)
            for event in pool.replacement_log()[before_events:]:
                events.append({"round": round_index, "page": labels[event.page_id],
                               "dirty": event.dirty, "policy": event.policy})
    finally:
        files.close()
    # 使用新的文件实例验证真正写入的内容，不通过原缓存读回。
    check_files = FileManager(path)
    try:
        check_pages = DiskPageManager(check_files)
        for label, value in expected.items():
            if COUNTER.unpack_from(check_pages.read_page(physical[label]), HEADER_SIZE)[0] != value:
                raise RuntimeError("缓存实验重开校验失败")
    finally:
        check_files.close()
    totals = {key: sum(sample[key] for sample in samples)
              for key in ("hits", "misses", "evictions", "writebacks")}
    totals["hit_rate"] = totals["hits"] / (len(accesses) * rounds)
    return {"policy": policy, "capacity": capacity, "scope": scope,
            "totals": totals, "rounds": samples, "replacement_log": events,
            "reopen_verified": True}


def run_experiment(*, work_dir: Path, accesses=(1, 2, 1, 3, 1, 2, 4, 1, 2, 3),
                   capacities=(2, 3, 4), write_pages=(1, 2), rounds=3):
    """同一序列、相同初始页、冷缓存起步；不访问用户数据库。"""
    accesses, capacities, write_pages = tuple(accesses), tuple(capacities), set(write_pages)
    if not accesses or any(type(page) is not int or page < 1 for page in accesses):
        raise ValueError("访问序列必须包含正整数页标签")
    if not capacities or any(type(size) is not int or size < 1 for size in capacities):
        raise ValueError("缓存容量必须为正整数")
    if type(rounds) is not int or rounds < 1:
        raise ValueError("轮数必须为正整数")
    if not write_pages.issubset(accesses):
        raise ValueError("写入页必须出现在访问序列中")
    results = []
    with TemporaryDirectory(prefix=".cache-experiment-", dir=work_dir) as directory:
        for scope in ("continuous", "reset-per-round"):
            for capacity in capacities:
                for policy in ("LRU", "FIFO"):
                    path = Path(directory) / f"case-{len(results)}.db"
                    results.append(_run_case(path, accesses, write_pages, capacity, policy, rounds, scope))
    return {
        "experiment": "page-cache-replay", "page_size": PAGE_SIZE,
        "accesses": list(accesses), "write_pages": sorted(write_pages), "round_count": rounds,
        "scope_description": {
            "continuous": "每轮刷新但持续保留同一缓存，可观察热缓存",
            "reset-per-round": "每轮刷新后重建缓存，模拟当前数据库跨事务的缓存生命周期",
        },
        "measurement": "独立页缓存实验，非 SQL 事务或端到端吞吐量测试；不计建页、重开校验及操作系统文件缓存",
        "writebacks_description": "成功写回的脏页次数（主动刷新及淘汰），不是 fsync 次数；每次访问写入页时计数器加一",
        "results": results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="MiniSQL LRU/FIFO 页缓存对比实验（使用独立临时文件）")
    parser.add_argument("--capacities", type=int, nargs="+", default=[2, 3, 4], help="比较的缓存页容量")
    parser.add_argument("--accesses", type=int, nargs="+", default=[1, 2, 1, 3, 1, 2, 4, 1, 2, 3], help="每轮访问的页标签序列")
    parser.add_argument("--write-pages", type=int, nargs="*", default=[1, 2], help="访问时修改的页；不提供值表示只读")
    parser.add_argument("--rounds", type=int, default=3, help="重复序列轮数")
    parser.add_argument("--work-dir", type=Path, default=Path.cwd(), help="已有的临时文件父目录，默认当前目录")
    args = parser.parse_args(argv)
    try:
        report = run_experiment(work_dir=args.work_dir, accesses=args.accesses,
                                capacities=args.capacities, write_pages=args.write_pages, rounds=args.rounds)
    except ValueError as error:
        parser.error(str(error))
    except (OSError, MiniSQLError) as error:
        print(f"缓存实验失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
