"""执行前兼容屏障：新语义不能被旧执行器静默忽略。"""
from dataclasses import fields, is_dataclass
from minisql.contracts.extensions import ExtendedPlan, Expr
from minisql.contracts.models import ColumnSchema, TableSchema, DataType
from minisql.contracts.errors import MiniSQLError, ErrorStage


def needs_extended(value):
    if isinstance(value, (ExtendedPlan, Expr)):
        return True
    if isinstance(value, ColumnSchema):
        return (value.data_type not in (DataType.INT, DataType.VARCHAR) or
                value.precision is not None or value.scale is not None or
                not value.nullable or value.has_default)
    if isinstance(value, TableSchema) and value.constraints:
        return True
    if is_dataclass(value):
        return any(needs_extended(getattr(value, f.name)) for f in fields(value) if not f.metadata.get('sensitive'))
    if isinstance(value, (tuple, list)):
        return any(needs_extended(item) for item in value)
    return False


def require_legacy_plan(plan):
    if needs_extended(plan):
        raise MiniSQLError(ErrorStage.EXECUTION, 'FEATURE_NOT_EXECUTABLE',
                           '扩展编译已完成；当前执行器/存储尚未接入此计划或类型')
