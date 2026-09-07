"""后续验收任务；实现真实测试操作和断言后，再移除 skip。"""
import pytest


@pytest.mark.skip(reason="compiler 业务尚未实现；负责人需替换失败占位体为真实验收")
@pytest.mark.parametrize("scenario", ["four_statement_ast","token_file_position","comments_strings_semicolon","not_and_or_precedence","missing_semicolon","unknown_column","type_mismatch","insert_column_reorder","constant_folding_equivalence","boolean_simplification_equivalence"])
def test_acceptance(scenario):
    pytest.fail(f"待编写真正的验收操作和断言: {scenario}")
