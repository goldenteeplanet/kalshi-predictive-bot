from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar


T = TypeVar("T")


@dataclass
class _Entry(Generic[T]):
    value: T
    loaded_at: float


class BoundedSingleFlightCache(Generic[T]):
    def __init__(self, *, ttl_seconds: float, max_entries: int = 8, wait_timeout_seconds: float = 5.0) -> None:
        if ttl_seconds <= 0 or max_entries <= 0 or wait_timeout_seconds <= 0:
            raise ValueError("cache bounds must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.wait_timeout_seconds = wait_timeout_seconds
        self._condition = threading.Condition()
        self._entries: OrderedDict[str, _Entry[T]] = OrderedDict()
        self._loading: set[str] = set()
        self._metrics = {"hits":0,"misses":0,"loads":0,"waits":0,"stale_fallbacks":0,"errors":0}

    def get(self, key: str, loader: Callable[[], T], *, force: bool = False) -> T:
        now = time.monotonic()
        with self._condition:
            entry = self._entries.get(key)
            if entry and not force and now - entry.loaded_at <= self.ttl_seconds:
                self._entries.move_to_end(key); self._metrics["hits"] += 1
                return entry.value
            self._metrics["misses"] += 1
            if key in self._loading:
                self._metrics["waits"] += 1
                deadline = now + self.wait_timeout_seconds
                while key in self._loading:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if entry:
                            self._metrics["stale_fallbacks"] += 1
                            return entry.value
                        raise TimeoutError("CACHE_REFRESH_TIMEOUT")
                    self._condition.wait(remaining)
                refreshed = self._entries.get(key)
                if refreshed:
                    return refreshed.value
            self._loading.add(key)
        try:
            value = loader()
        except Exception:
            with self._condition:
                self._loading.discard(key); self._metrics["errors"] += 1; self._condition.notify_all()
                stale = self._entries.get(key)
                if stale:
                    self._metrics["stale_fallbacks"] += 1
                    return stale.value
            raise
        with self._condition:
            self._entries[key] = _Entry(value=value, loaded_at=time.monotonic())
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            self._loading.discard(key); self._metrics["loads"] += 1; self._condition.notify_all()
        return value

    def clear(self) -> None:
        with self._condition:
            self._entries.clear()

    def metrics(self) -> dict[str, int | float]:
        with self._condition:
            return {**self._metrics,"entries":len(self._entries),"inflight":len(self._loading),"max_entries":self.max_entries,"ttl_seconds":self.ttl_seconds}
