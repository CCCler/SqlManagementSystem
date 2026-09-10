"""结果展示与跟踪序列化的前瞻验收（新类型契约生效前的展示层行为）。"""
import json
from datetime import date, datetime, time
from decimal import Decimal

from minisql.cli.main import format_value, render_result
from minisql.cli.trace import to_json_value
from minisql.contracts.models import ExecutionResult


def test_format_value_covers_future_value_domain():
    assert format_value(None) == "NULL"
    assert format_value(True) == "TRUE"
    assert format_value(False) == "FALSE"
    assert format_value(Decimal("123.45")) == "123.45"
    assert format_value(date(2026, 9, 10)) == "2026-09-10"
    assert format_value(time(12, 30, 5)) == "12:30:05"
    assert format_value(datetime(2026, 9, 10, 12, 30, 5)) == "2026-09-10 12:30:05"
    assert format_value(42) == "42"
    assert format_value("中文") == "中文"


def test_render_result_displays_new_values():
    result = ExecutionResult(
        columns=("id", "name", "note", "flag"),
        rows=((1, None, Decimal("9.50"), True),),
    )
    rendered = render_result(result)
    assert "NULL" in rendered
    assert "9.50" in rendered
    assert "TRUE" in rendered


def test_render_result_aligns_with_formatted_widths():
    result = ExecutionResult(
        columns=("v",),
        rows=((None,), ("value",)),
    )
    lines = render_result(result).splitlines()
    assert lines[0].startswith("v")
    assert lines[2].startswith("NULL ")
    assert lines[3].startswith("value")


def test_trace_serializes_new_values_as_json():
    payload = to_json_value({
        "n": None,
        "d": Decimal("1.50"),
        "t": datetime(2026, 9, 10, 8, 0),
        "day": date(2026, 9, 10),
    })
    text = json.dumps(payload, ensure_ascii=False)
    assert json.loads(text) == {
        "n": None,
        "d": "1.50",
        "t": "2026-09-10T08:00:00",
        "day": "2026-09-10",
    }
