"""
In-memory TTL cache with single-flight request deduplication for analytical endpoints.
Prevents duplicate heavy calculations on the same Uvicorn worker.
"""
import asyncio
import time
import logging
from typing import Any, Callable, Coroutine, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

class AnalyticsCache:
    """
    Worker-level TTL Cache with In-Flight single-flight protection and bounded size.
    """
    def __init__(self, default_ttl: int = 30, max_entries: int = 100):
        self.default_ttl = default_ttl
        self.max_entries = max_entries
        self._store: dict[str, tuple[float, Any]] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    def _clean_expired(self):
        now = time.monotonic()
        expired = [k for k, (exp, _) in self._store.items() if now > exp]
        for k in expired:
            del self._store[k]

        # Enforce maximum cache entries (evict oldest if above max_entries)
        if len(self._store) > self.max_entries:
            # Sort by expiration time and remove the earliest expiring entries
            sorted_keys = sorted(self._store.keys(), key=lambda k: self._store[k][0])
            to_remove = sorted_keys[: len(self._store) - self.max_entries]
            for k in to_remove:
                del self._store[k]

    async def get_or_compute(
        self,
        key: str,
        compute_fn: Callable[[], Coroutine[Any, Any, T]],
        ttl: int | None = None
    ) -> tuple[T, bool]:
        """
        Returns (result, cache_hit).
        If result is cached, returns (result, True).
        If request is in-flight, awaits the in-flight task and returns (result, True).
        Otherwise, computes result, caches it for `ttl` seconds, and returns (result, False).
        """
        effective_ttl = ttl if ttl is not None else self.default_ttl
        now = time.monotonic()

        async with self._lock:
            self._clean_expired()

            # 1. Check valid cache entry
            if key in self._store:
                exp, val = self._store[key]
                if now <= exp:
                    return val, True
                else:
                    del self._store[key]

            # 2. Check if identical request is already in-flight
            if key in self._inflight:
                task = self._inflight[key]
                pass

        if key in self._inflight:
            try:
                val = await self._inflight[key]
                return val, True
            except Exception:
                # If in-flight task failed, fallback to computing
                pass

        # 3. Register new in-flight task under lock
        async with self._lock:
            # Re-check cache in case completed while waiting for lock
            if key in self._store:
                exp, val = self._store[key]
                if now <= exp:
                    return val, True

            task = asyncio.create_task(compute_fn())
            self._inflight[key] = task

        try:
            val = await task
            async with self._lock:
                self._store[key] = (time.monotonic() + effective_ttl, val)
                self._clean_expired()
            return val, False
        finally:
            async with self._lock:
                if key in self._inflight and self._inflight[key] == task:
                    del self._inflight[key]

    def clear(self):
        self._store.clear()
        self._inflight.clear()


# Global cache instance for worker (bounded to 100 entries, 30s TTL)
analytics_cache = AnalyticsCache(default_ttl=30, max_entries=100)
