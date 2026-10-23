"""A tiny in-process TTL cache for data read on many requests and changed rarely (store settings,
active delivery methods).

Per worker process: a change clears this process's copy (CatalogChanged, scripts); other workers
converge within LOCAL_TTL_S. Never used by checkout / order placement, which always read fresh.
"""

import time
from collections.abc import Callable
from typing import Any

LOCAL_TTL_S = 60.0


class LocalTTLCache:
    def __init__(self, ttl_seconds: float = LOCAL_TTL_S, now: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl_seconds
        self.now = now
        self._items: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires, value = item
        if expires <= self.now():
            self._items.pop(key, None)
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        self._items[key] = (self.now() + self.ttl, value)

    def clear(self) -> None:
        self._items.clear()


local_cache = LocalTTLCache()
