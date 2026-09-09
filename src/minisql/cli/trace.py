"""编译跟踪：在真实编译调用完成后输出 JSON Lines，不重新编译或执行。"""
from dataclasses import fields, is_dataclass
from enum import Enum
import json


def to_json_value(value):
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {"node": type(value).__name__, **{
            field.name: to_json_value(getattr(value, field.name)) for field in fields(value)}}
    if isinstance(value, (tuple, list)):
        return [to_json_value(item) for item in value]
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
            for name in ("tokens", "ast", "semantic", "plan", "optimized_plan")})
        return result
