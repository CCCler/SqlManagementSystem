"""SQL 扩展阶段牵头契约的接口示例与内存替身。

实现 docs/SQL扩展接口示例-成员三.md 提议的 ObjectCatalog / DependencyTracker
语义；契约评审通过后，正式类型移入 contracts/、真实实现落 engine/，
本替身继续作为隔离测试夹具。"""
from dataclasses import dataclass

from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.models import ColumnSchema


def _error(stage: ErrorStage, code: str, reason: str) -> MiniSQLError:
    return MiniSQLError(stage, code, reason)


@dataclass(frozen=True)
class ViewDefinition:
    """视图定义：规范化 SQL 文本 + 编译后的输出列清单。"""

    name: str
    definition: str
    columns: tuple[ColumnSchema, ...]


@dataclass(frozen=True)
class TriggerDefinition:
    """触发器定义：AFTER 行级，同一事件多个触发器按 created_order 升序执行。"""

    name: str
    table: str
    event: str  # INSERT / UPDATE / DELETE
    action: str
    created_order: int = 0


@dataclass(frozen=True)
class IndexDefinition:
    """索引定义：单列或联合列，唯一索引另设 unique 标志。"""

    name: str
    table: str
    columns: tuple[str, ...]
    unique: bool = False


class MemoryObjectCatalog:
    """ObjectCatalog 提议接口的内存实现：视图/触发器/索引定义的管理。

    名称统一小写键、大小写不敏感；重名注册报 DUPLICATE_OBJECT；
    注销不存在的对象报 UNKNOWN_OBJECT。持久化实现按 __views/__triggers/
    __indexes 系统表布局落地后替换本替身，语义不变。
    """

    def __init__(self):
        self._views: dict[str, ViewDefinition] = {}
        self._triggers: dict[str, TriggerDefinition] = {}
        self._indexes: dict[str, IndexDefinition] = {}

    # ---- 视图 ----
    def register_view(self, view: ViewDefinition) -> None:
        key = view.name.lower()
        if key in self._views:
            raise _error(ErrorStage.SEMANTIC, "DUPLICATE_OBJECT", f"视图 {view.name} 已存在")
        self._views[key] = view

    def get_view(self, name: str) -> ViewDefinition | None:
        return self._views.get(name.lower())

    def list_views(self) -> tuple[ViewDefinition, ...]:
        return tuple(self._views.values())

    def unregister_view(self, name: str) -> None:
        if self._views.pop(name.lower(), None) is None:
            raise _error(ErrorStage.SEMANTIC, "UNKNOWN_OBJECT", f"视图 {name} 不存在")

    # ---- 触发器 ----
    def register_trigger(self, trigger: TriggerDefinition) -> None:
        key = trigger.name.lower()
        if key in self._triggers:
            raise _error(ErrorStage.SEMANTIC, "DUPLICATE_OBJECT", f"触发器 {trigger.name} 已存在")
        self._triggers[key] = trigger

    def get_trigger(self, name: str) -> TriggerDefinition | None:
        return self._triggers.get(name.lower())

    def get_triggers(self, table: str, event: str) -> tuple[TriggerDefinition, ...]:
        """指定表的指定事件的全部触发器，按创建先后排序。"""
        key = table.lower()
        matched = [t for t in self._triggers.values() if t.table == key and t.event == event.upper()]
        return tuple(sorted(matched, key=lambda trigger: trigger.created_order))

    def unregister_trigger(self, name: str) -> None:
        if self._triggers.pop(name.lower(), None) is None:
            raise _error(ErrorStage.SEMANTIC, "UNKNOWN_OBJECT", f"触发器 {name} 不存在")

    # ---- 索引 ----
    def register_index(self, index: IndexDefinition) -> None:
        key = index.name.lower()
        if key in self._indexes:
            raise _error(ErrorStage.SEMANTIC, "DUPLICATE_OBJECT", f"索引 {index.name} 已存在")
        self._indexes[key] = index

    def get_index(self, name: str) -> IndexDefinition | None:
        return self._indexes.get(name.lower())

    def get_indexes(self, table: str) -> tuple[IndexDefinition, ...]:
        key = table.lower()
        return tuple(index for index in self._indexes.values() if index.table == key)

    def unregister_index(self, name: str) -> None:
        if self._indexes.pop(name.lower(), None) is None:
            raise _error(ErrorStage.SEMANTIC, "UNKNOWN_OBJECT", f"索引 {name} 不存在")


class DependencyTracker:
    """__dependencies 提议接口的内存实现；对象以 (类型, 名称) 标识。

    用于视图/触发器的依赖保护：DROP/ALTER 前调用 assert_droppable，
    存在依赖者时报 DEPENDENT_OBJECT 并拒绝操作（首版不隐式级联）。
    """

    def __init__(self):
        self._deps: dict[tuple[str, str], set[tuple[str, str]]] = {}

    def add_dependency(self, object_type: str, object_name: str,
                       depends_on_type: str, depends_on_name: str) -> None:
        """登记：object 依赖 depends_on。"""
        key = (object_type, object_name.lower())
        self._deps.setdefault(key, set()).add((depends_on_type, depends_on_name.lower()))

    def remove_object(self, object_type: str, object_name: str) -> None:
        """对象删除/重建时移除它自身的全部依赖记录。"""
        self._deps.pop((object_type, object_name.lower()), None)

    def dependencies(self, object_type: str, object_name: str) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._deps.get((object_type, object_name.lower()), ())))

    def dependents(self, object_type: str, object_name: str) -> tuple[tuple[str, str], ...]:
        """依赖该对象的全部对象（有序，便于测试与报错信息稳定）。"""
        target = (object_type, object_name.lower())
        return tuple(sorted(key for key, deps in self._deps.items() if target in deps))

    def assert_droppable(self, object_type: str, object_name: str) -> None:
        dependents = self.dependents(object_type, object_name)
        if dependents:
            raise _error(
                ErrorStage.SEMANTIC, "DEPENDENT_OBJECT",
                f"{object_type} {object_name} 被依赖对象使用：{dependents}",
            )
