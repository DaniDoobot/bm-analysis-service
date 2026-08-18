"""
Unit tests for memory monitoring utility and bounded analytics cache.
"""
import asyncio
import os
import unittest
from unittest.mock import patch, MagicMock

from app.utils.memory_utils import (
    get_process_rss_mb,
    log_process_memory,
    check_and_log_memory_thresholds,
    track_memory_async,
    track_memory_sync,
    MEMORY_WARN_THRESHOLD_MB,
    MEMORY_CRITICAL_THRESHOLD_MB,
)
from app.utils.cache import AnalyticsCache


class TestMemoryUtils(unittest.TestCase):
    def test_get_process_rss_mb_returns_positive_float(self):
        rss = get_process_rss_mb()
        self.assertIsInstance(rss, float)
        self.assertGreater(rss, 0.0, "Process RSS memory should be greater than 0 MB")

    def test_log_process_memory_logs_without_errors(self):
        rss = log_process_memory("test_tag")
        self.assertIsInstance(rss, float)
        self.assertGreater(rss, 0.0)

    def test_check_and_log_memory_thresholds(self):
        # Normal memory
        with self.assertLogs("app.memory", level="INFO") as cm:
            log_process_memory("normal_tag", warn_threshold_mb=10000.0)
            self.assertTrue(any("normal_tag" in log for log in cm.output))

        # Memory warning threshold (> 750 MB)
        with self.assertLogs("app.memory", level="WARNING") as cm_warn:
            check_and_log_memory_thresholds(800.0, tag="test_warn", warn_threshold_mb=750.0, crit_threshold_mb=1000.0)
            self.assertTrue(any("memory_warning" in log for log in cm_warn.output))

        # Memory critical threshold (> 1000 MB)
        with self.assertLogs("app.memory", level="ERROR") as cm_crit:
            check_and_log_memory_thresholds(1100.0, tag="test_crit", warn_threshold_mb=750.0, crit_threshold_mb=1000.0)
            self.assertTrue(any("memory_critical" in log for log in cm_crit.output))

    def test_track_memory_sync(self):
        with track_memory_sync("test_sync_block") as metrics:
            a = [i for i in range(10000)]
            self.assertIn("start_rss_mb", metrics)

        self.assertGreater(metrics["duration_s"], 0.0)
        self.assertGreater(metrics["end_rss_mb"], 0.0)


class TestMemoryUtilsAsync(unittest.IsolatedAsyncioTestCase):
    async def test_track_memory_async(self):
        async with track_memory_async("test_async_block") as metrics:
            await asyncio.sleep(0.01)
            self.assertIn("start_rss_mb", metrics)

        self.assertGreaterEqual(metrics["duration_s"], 0.01)
        self.assertGreater(metrics["end_rss_mb"], 0.0)

    async def test_analytics_cache_bounded_max_entries(self):
        cache = AnalyticsCache(default_ttl=30, max_entries=5)

        # Insert 10 entries
        for i in range(10):
            async def compute(val=i):
                return f"value_{val}"
            res, hit = await cache.get_or_compute(f"key_{i}", compute)
            self.assertEqual(res, f"value_{i}")
            self.assertFalse(hit)

        # Store should be bounded to at most max_entries (5)
        self.assertLessEqual(len(cache._store), 5)

        # Clearing cache
        cache.clear()
        self.assertEqual(len(cache._store), 0)


if __name__ == "__main__":
    unittest.main()
