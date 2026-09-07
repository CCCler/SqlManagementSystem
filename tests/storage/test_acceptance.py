"""后续验收任务；实现真实测试操作和断言后，再移除 skip。"""
import pytest


@pytest.mark.skip(reason="storage 业务尚未实现；负责人需替换失败占位体为真实验收")
@pytest.mark.parametrize("scenario", ["allocate_free_reuse_page","cross_page_scan","unicode_row_roundtrip","oversized_row_rejected","lru_replacement","fifo_replacement","dirty_eviction_flush","hit_statistics","reopen_records"])
def test_acceptance(scenario):
    pytest.fail(f"待编写真正的验收操作和断言: {scenario}")
