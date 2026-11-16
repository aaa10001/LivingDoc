"""Webhook 投递去重（进程内占位实现）。

正式方案见 ADR-003：webhook_deliveries 表的 (project_id, dedupe_key) 唯一索引。
这里先给一个 TTL + 容量上限的进程内缓存，保证阶段二单进程演示时不会重复触发；
多 worker / 多实例下**不保证**，阶段三接上数据库后删掉即可。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

from app.config import get_settings


class TTLSet:
    """带过期时间和容量上限的集合，线程安全。"""

    def __init__(self, ttl_seconds: int, max_items: int) -> None:
        self._ttl = ttl_seconds
        self._max = max_items
        self._items: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, key: str) -> bool:
        """返回 True 表示首次出现（应当处理），False 表示重复。"""
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            if key in self._items:
                self._items.move_to_end(key)
                return False
            self._items[key] = now + self._ttl
            while len(self._items) > self._max:
                self._items.popitem(last=False)
            return True

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._items

    def _purge(self, now: float) -> None:
        # OrderedDict 按插入顺序排列，过期项永远在头部，扫到第一个未过期就停
        while self._items:
            oldest_key = next(iter(self._items))
            if self._items[oldest_key] > now:
                break
            self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_settings = get_settings()
delivery_cache = TTLSet(_settings.dedupe_ttl_seconds, _settings.dedupe_max_items)
