from typing import Literal
from minisql.contracts.interfaces import PageManager
from minisql.contracts.models import CacheStats, ReplacementEvent


class PageBufferPool:
    def __init__(self, pages: PageManager, capacity: int = 64,
                 policy: Literal["LRU", "FIFO"] = "LRU") -> None:
        self.pages = pages
        self.capacity = capacity
        self.policy = policy

    def get_page(self, page_id: int) -> bytearray:
        raise NotImplementedError("成员二：缓存访问与替换")

    def mark_dirty(self, page_id: int) -> None:
        raise NotImplementedError("成员二：标记脏页")

    def flush_page(self, page_id: int) -> None:
        raise NotImplementedError("成员二：刷新指定页")

    def flush_all(self) -> None:
        raise NotImplementedError("成员二：刷新全部脏页")

    def stats(self) -> CacheStats:
        raise NotImplementedError("成员二：缓存统计")

    def replacement_log(self) -> tuple[ReplacementEvent, ...]:
        raise NotImplementedError("成员二：替换日志")
