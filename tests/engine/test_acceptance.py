"""后续验收任务；实现真实测试操作和断言后，再移除 skip。"""
import pytest


@pytest.mark.skip(reason="engine 业务尚未实现；负责人需替换失败占位体为真实验收")
@pytest.mark.parametrize("scenario", ["manual_create_insert_select_delete","catalog_bootstrap_and_reload","filter_retains_record_id","unknown_plan_error"])
def test_acceptance(scenario):
    pytest.fail(f"待编写真正的验收操作和断言: {scenario}")
