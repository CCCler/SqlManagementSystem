"""编译跟踪：在真实编译调用完成后输出 JSON Lines，不重新编译或执行。"""
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
import json


def to_json_value(value):
    if value is None:
        return None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if is_dataclass(value):
        return {"node": type(value).__name__, **{
            field.name: to_json_value(getattr(value, field.name)) for field in fields(value)
            if not field.metadata.get("sensitive")}}
    if isinstance(value, (tuple, list)):
        return [to_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: to_json_value(item) for key, item in value.items()}
    return value


def emit_event(event, **payload):
    print(json.dumps({"event": event, **payload}, ensure_ascii=False))


class TracingCompiler:
    def __init__(self, compiler):
        self.compiler = compiler

    def split_statements(self, sql):
        return self.compiler.split_statements(sql)

    def compile(self, sql, catalog):
        result = self.compiler.compile(sql, catalog)
        emit_event("compilation", **{
            name: to_json_value(getattr(result, name))
            for name in ("tokens", "ast", "semantic", "plan", "optimized_plan", "output_fields", "dependencies", "required_capabilities")})
        return result
