"""Bounded, credential-free performance samples used only by background work."""
from __future__ import annotations

import math
import time
from collections import deque
from functools import wraps
from threading import Lock


class PressureSamples:
    def __init__(self, *, window_seconds=60, capacity=512):
        self.window_seconds = window_seconds
        self.capacity = capacity
        self._lock = Lock()
        self._samples = {}

    def observe(self, metric, value, *, now=None):
        if metric not in {"database_ms", "database_error", "writer_wait_ms", "upstream_ms", "upstream_error", "queue_ms"}:
            return
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return
        if not math.isfinite(value) or value < 0:
            return
        with self._lock:
            self._samples.setdefault(metric, deque(maxlen=self.capacity)).append((time.monotonic() if now is None else now, value))

    def snapshot(self, *, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            recent = {key: [(stamp, value) for stamp, value in values if 0 <= now - stamp <= self.window_seconds]
                      for key, values in self._samples.items()}
        result = {}
        for key, values in recent.items():
            if not values:
                continue
            ordered = sorted(value for _, value in values)
            result[key] = {"count": len(values), "p95": ordered[max(0, math.ceil(len(values) * .95) - 1)],
                           "mean": sum(ordered) / len(ordered), "latest": values[-1][1],
                           "age_seconds": max(0, now - values[-1][0])}
        return result


pressure_samples = PressureSamples()


def measure_account_upstream(function):
    """Measure account checks/OAuth, NOT long-running image generation."""
    @wraps(function)
    def measured(*args, **kwargs):
        began = time.perf_counter()
        failed = False
        try:
            return function(*args, **kwargs)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            # Bad credentials are inventory failures, not evidence of server load.
            credential_error = type(exc).__name__ in {"InvalidAccessTokenError", "TerminalRefreshTokenError", "RefreshCredentialsChangedError"}
            failed = not credential_error and (status == 429 or (isinstance(status, int) and status >= 500) or status is None)
            raise
        finally:
            pressure_samples.observe("upstream_ms", (time.perf_counter() - began) * 1000)
            pressure_samples.observe("upstream_error", int(failed))
    return measured


def local_pressure_snapshot():
    # Cached platform/cgroup sampling; no account/history/database SELECT.
    from services.runtime_environment_service import snapshot
    from services.application_database import database_pool_utilization
    runtime = snapshot()
    return {"schema": 1, "sampled_at": time.time(), "samples": pressure_samples.snapshot(),
            "cpu_percent": runtime.get("process_cpu_percent"),
            "memory_percent": runtime.get("memory_percent"),
            "database_pool_utilization": database_pool_utilization()}
