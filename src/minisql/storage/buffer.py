"""页缓冲池：LRU / FIFO 替换、脏页写回、命中统计与替换日志。"""
from collections import OrderedDict
from typing import Literal

from minisql.contracts.errors import ErrorStage, MiniSQLError
from minisql.contracts.interfaces import PageManager
from minisql.contracts.models import CacheStats, ReplacementEvent


class PageBufferPool:
    """以 OrderedDict 维护访问顺序，统一支持 LRU 与 FIFO 两种替换策略。

    - LRU：get_page 命中时把页移到末尾，淘汰最左（最久未使用）。
    - FIFO：get_page 命中时不动，淘汰最左（最先插入）。
    """

    def __init__(self, pages: PageManager, capacity: int = 64,
                 policy: Literal["LRU", "FIFO"] = "LRU") -> None:
        if capacity < 1:
            raise MiniSQLError(ErrorStage.STORAGE, "INVALID_CONFIG", "缓存容量必须大于 0")
        self.pages = pages
        self.capacity = capacity
        self.policy = policy
        self._cache: OrderedDict[int, bytearray] = OrderedDict()
        self._dirty: set[int] = set()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._writebacks = 0
        self._replacement_log: list[ReplacementEvent] = []

    def get_page(self, page_id: int) -> bytearray:
        if page_id in self._cache:
            self._hits += 1
            if self.policy == "LRU":
                self._cache.move_to_end(page_id)
            return self._cache[page_id]

        self._misses += 1
        page = bytearray(self.pages.read_page(page_id))

        if len(self._cache) >= self.capacity:
            self._evict_one()

        self._cache[page_id] = page
        return page

    def mark_dirty(self, page_id: int) -> None:
        if page_id not in self._cache:
            raise MiniSQLError(
                ErrorStage.STORAGE, "IO_ERROR", f"页 {page_id} 不在缓存中，无法标记脏页")
        self._dirty.add(page_id)

    def flush_page(self, page_id: int) -> None:
        if page_id in self._dirty:
            self.pages.write_page(page_id, bytes(self._cache[page_id]))
            self._writebacks += 1
            self._dirty.discard(page_id)

    def flush_all(self) -> None:
        for page_id in list(self._dirty):
            self.flush_page(page_id)

    def stats(self) -> CacheStats:
        return CacheStats(self._hits, self._misses, self._evictions)

    @property
    def writebacks(self) -> int:
        """成功写回的脏页次数，包含主动刷新及淘汰，不等同于 fsync 次数。"""
        return self._writebacks

    def discard_page(self, page_id: int) -> None:
        """丢弃即将释放页的缓存及脏标记，不写回已作废的数据。"""
        self._cache.pop(page_id, None)
        self._dirty.discard(page_id)

    def replacement_log(self) -> tuple[ReplacementEvent, ...]:
        return tuple(self._replacement_log)

    def _evict_one(self) -> None:
        victim = next(iter(self._cache))
        page = self._cache[victim]
        dirty = victim in self._dirty
        if dirty:
            self.pages.write_page(victim, bytes(page))
            self._writebacks += 1
            self._dirty.discard(victim)
        del self._cache[victim]  # 写回失败时保留脏页，允许调用者重试。
        self._evictions += 1
        self._replacement_log.append(ReplacementEvent(victim, dirty, self.policy))
