"""后续验收任务；实现真实测试操作和断言后，再移除 skip。"""
import pytest


@pytest.mark.skip(reason="integration 业务尚未实现；负责人需替换失败占位体为真实验收")
@pytest.mark.parametrize("scenario", ["core_sql_sequence","restart_data_and_catalog","multi_statement_create_then_insert","stop_after_error","whole_file_error_position"])
def test_acceptance(scenario):
    pytest.fail(f"待编写真正的验收操作和断言: {scenario}")
