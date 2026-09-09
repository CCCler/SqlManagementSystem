import json

import pytest

from minisql.cli.cache_experiment import main, run_experiment
from minisql.storage.buffer import PageBufferPool
from minisql.storage.file_manager import FileManager
from minisql.storage.page import DiskPageManager, PAGE_SIZE


def test_known_lru_fifo_counts_and_scope_difference(tmp_path):
    report = run_experiment(work_dir=tmp_path, accesses=(1, 2, 1, 3, 1),
                            capacities=(2, 3), write_pages=(), rounds=2)
    cases = {(c["scope"], c["capacity"], c["policy"]): c for c in report["results"]}
    assert len(cases) == 8
    for policy, hits, misses in [("LRU", 2, 3), ("FIFO", 1, 4)]:
        cold = cases["reset-per-round", 2, policy]
        assert cold["totals"]["hits"] == hits * 2
        assert cold["totals"]["misses"] == misses * 2
        assert cold["totals"]["writebacks"] == 0
    assert cases["continuous", 3, "LRU"]["rounds"][1]["misses"] == 0
    assert cases["reset-per-round", 3, "LRU"]["rounds"][1]["misses"] == 3
    assert cases["continuous", 3, "LRU"]["totals"]["hit_rate"] == 0.7
    assert all(c["reopen_verified"] for c in report["results"])
    assert list(tmp_path.iterdir()) == []


def test_writebacks_include_eviction_and_end_of_round_flush(tmp_path):
    report = run_experiment(work_dir=tmp_path, accesses=(1, 2, 1), capacities=(1, 2),
                            write_pages=(1,), rounds=1)
    for case in report["results"]:
        assert case["totals"]["writebacks"] == (2 if case["capacity"] == 1 else 1)
        if case["capacity"] == 1:
            assert [event["dirty"] for event in case["replacement_log"]] == [True, False]
            assert [event["page"] for event in case["replacement_log"]] == [1, 2]
        assert case["reopen_verified"]


@pytest.mark.parametrize("kwargs", [
    {"accesses": ()}, {"accesses": (0,)}, {"capacities": (0,)},
    {"capacities": ()}, {"rounds": 0}, {"write_pages": (99,)},
])
def test_invalid_experiment_arguments_do_not_create_files(tmp_path, kwargs):
    with pytest.raises(ValueError):
        run_experiment(work_dir=tmp_path, **kwargs)
    assert list(tmp_path.iterdir()) == []


def test_experiment_cli_json_and_readonly_option(tmp_path, capsys):
    assert main(["--work-dir", str(tmp_path), "--capacities", "1", "2", "--rounds", "1", "--write-pages"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert len(report["results"]) == 8
    assert all(case["totals"]["writebacks"] == 0 for case in report["results"])
    assert list(tmp_path.iterdir()) == []


def test_failed_dirty_writeback_is_not_counted_or_lost(tmp_path, monkeypatch):
    files = FileManager(tmp_path / "pages.db")
    try:
        pages = DiskPageManager(files)
        for page in (1, 2):
            pages.write_page(page, bytes(PAGE_SIZE))
        pool = PageBufferPool(pages, capacity=1)
        pool.get_page(1)[100] = 17
        pool.mark_dirty(1)
        original = pages.write_page
        def fail(*args):
            raise OSError("injected")
        monkeypatch.setattr(pages, "write_page", fail)
        with pytest.raises(OSError):
            pool.get_page(2)
        assert pool.writebacks == 0
        assert pool.stats().evictions == 0
        assert pool.replacement_log() == ()
        assert pool.get_page(1)[100] == 17
        monkeypatch.setattr(pages, "write_page", original)
        pool.get_page(2)
        assert pool.writebacks == 1
        assert pages.read_page(1)[100] == 17
        pool.flush_all()
        assert pool.writebacks == 1
    finally:
        files.close()
