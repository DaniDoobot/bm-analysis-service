"""
Lightweight process RSS memory monitoring and tracking utility.
Provides cross-platform RSS memory lookup (Linux /proc, getrusage, psutil, Windows ctypes/psapi),
threshold warnings (>750 MB, >1000 MB), and context managers for measuring memory spikes.
"""
import os
import sys
import gc
import time
import logging
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncGenerator, Generator

logger = logging.getLogger("app.memory")

# Memory thresholds in megabytes (MB)
MEMORY_WARN_THRESHOLD_MB = 750.0
MEMORY_CRITICAL_THRESHOLD_MB = 1000.0
MEMORY_SPIKE_DELTA_WARN_MB = 50.0


def get_process_rss_mb() -> float:
    """
    Returns current process RSS (Resident Set Size) in megabytes (MB).
    Fast, lightweight, no mandatory third-party dependencies.
    """
    # 1. Try psutil if installed
    try:
        import psutil  # type: ignore
        process = psutil.Process()
        return process.memory_info().rss / (1024.0 * 1024.0)
    except Exception:
        pass

    # 2. Linux /proc/self/status (most efficient and accurate on Linux containers)
    if os.path.exists("/proc/self/status"):
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            # VmRSS is in kB
                            return float(parts[1]) / 1024.0
        except Exception:
            pass

    # 3. Unix resource.getrusage
    try:
        import resource  # type: ignore
        rusage = resource.getrusage(resource.RUSAGE_SELF)
        # On Linux ru_maxrss is in kilobytes; on macOS it is in bytes
        if sys.platform == "darwin":
            return rusage.ru_maxrss / (1024.0 * 1024.0)
        else:
            return rusage.ru_maxrss / 1024.0
    except Exception:
        pass

    # 4. Windows ctypes GetProcessMemoryInfo
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), ctypes.sizeof(counters)
            ):
                return counters.WorkingSetSize / (1024.0 * 1024.0)
        except Exception:
            pass

    return 0.0


def check_and_log_memory_thresholds(
    rss_mb: float,
    tag: str = "",
    warn_threshold_mb: float = MEMORY_WARN_THRESHOLD_MB,
    crit_threshold_mb: float = MEMORY_CRITICAL_THRESHOLD_MB,
) -> None:
    """Logs warnings or critical errors if RSS memory crosses thresholds."""
    pid = os.getpid()
    prefix = f"[{tag}] " if tag else ""
    if rss_mb >= crit_threshold_mb:
        logger.error(
            "[memory_critical] %spid=%d RSS=%.1f MB exceeded critical threshold of %.1f MB! "
            "Triggering immediate GC collect.",
            prefix,
            pid,
            rss_mb,
            crit_threshold_mb,
        )
        gc.collect()
    elif rss_mb >= warn_threshold_mb:
        logger.warning(
            "[memory_warning] %spid=%d RSS=%.1f MB exceeded warning threshold of %.1f MB.",
            prefix,
            pid,
            rss_mb,
            warn_threshold_mb,
        )


def log_process_memory(
    tag: str,
    level: int = logging.INFO,
    warn_threshold_mb: float = MEMORY_WARN_THRESHOLD_MB,
    crit_threshold_mb: float = MEMORY_CRITICAL_THRESHOLD_MB,
) -> float:
    """
    Logs current RSS memory with tag and pid.
    Also checks warning / critical thresholds.
    """
    rss = get_process_rss_mb()
    pid = os.getpid()
    logger.log(level, "[memory] tag=%s pid=%d rss=%.1f MB", tag, pid, rss)
    check_and_log_memory_thresholds(rss, tag, warn_threshold_mb, crit_threshold_mb)
    return rss


@asynccontextmanager
async def track_memory_async(
    tag: str,
    warn_delta_mb: float = MEMORY_SPIKE_DELTA_WARN_MB,
    warn_threshold_mb: float = MEMORY_WARN_THRESHOLD_MB,
    crit_threshold_mb: float = MEMORY_CRITICAL_THRESHOLD_MB,
) -> AsyncGenerator[dict[str, Any], None]:
    """
    Async context manager that tracks memory delta and duration for an async operation.
    Logs warning if operation causes a memory spike (>50 MB) or if total RSS is high.
    """
    pid = os.getpid()
    rss_start = get_process_rss_mb()
    t_start = time.perf_counter()
    metrics = {"start_rss_mb": rss_start, "end_rss_mb": 0.0, "delta_mb": 0.0, "duration_s": 0.0}
    try:
        yield metrics
    finally:
        t_end = time.perf_counter()
        rss_end = get_process_rss_mb()
        delta = rss_end - rss_start
        duration = t_end - t_start
        metrics["end_rss_mb"] = rss_end
        metrics["delta_mb"] = delta
        metrics["duration_s"] = duration

        if delta >= warn_delta_mb:
            logger.warning(
                "[memory_spike] tag=%s pid=%d rss_start=%.1f MB rss_end=%.1f MB delta=%+.1f MB duration=%.2fs",
                tag,
                pid,
                rss_start,
                rss_end,
                delta,
                duration,
            )
        else:
            logger.info(
                "[memory_track] tag=%s pid=%d rss_start=%.1f MB rss_end=%.1f MB delta=%+.1f MB duration=%.2fs",
                tag,
                pid,
                rss_start,
                rss_end,
                delta,
                duration,
            )

        check_and_log_memory_thresholds(rss_end, tag, warn_threshold_mb, crit_threshold_mb)
        # If memory is elevated, run GC to reclaim unreferenced cycles immediately
        if rss_end >= warn_threshold_mb:
            gc.collect()


@contextmanager
def track_memory_sync(
    tag: str,
    warn_delta_mb: float = MEMORY_SPIKE_DELTA_WARN_MB,
    warn_threshold_mb: float = MEMORY_WARN_THRESHOLD_MB,
    crit_threshold_mb: float = MEMORY_CRITICAL_THRESHOLD_MB,
) -> Generator[dict[str, Any], None, None]:
    """
    Sync context manager for tracking memory delta and duration.
    """
    pid = os.getpid()
    rss_start = get_process_rss_mb()
    t_start = time.perf_counter()
    metrics = {"start_rss_mb": rss_start, "end_rss_mb": 0.0, "delta_mb": 0.0, "duration_s": 0.0}
    try:
        yield metrics
    finally:
        t_end = time.perf_counter()
        rss_end = get_process_rss_mb()
        delta = rss_end - rss_start
        duration = t_end - t_start
        metrics["end_rss_mb"] = rss_end
        metrics["delta_mb"] = delta
        metrics["duration_s"] = duration

        if delta >= warn_delta_mb:
            logger.warning(
                "[memory_spike] tag=%s pid=%d rss_start=%.1f MB rss_end=%.1f MB delta=%+.1f MB duration=%.2fs",
                tag,
                pid,
                rss_start,
                rss_end,
                delta,
                duration,
            )
        else:
            logger.info(
                "[memory_track] tag=%s pid=%d rss_start=%.1f MB rss_end=%.1f MB delta=%+.1f MB duration=%.2fs",
                tag,
                pid,
                rss_start,
                rss_end,
                delta,
                duration,
            )

        check_and_log_memory_thresholds(rss_end, tag, warn_threshold_mb, crit_threshold_mb)
        if rss_end >= warn_threshold_mb:
            gc.collect()
