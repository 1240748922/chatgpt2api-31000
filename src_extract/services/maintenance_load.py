"""Shared load gate for non-user-facing account maintenance work.

Maintenance must yield to image traffic.  The image worker pool is deliberately
not throttled here; this module only decides whether background work should
start at the current moment.
"""

from __future__ import annotations

from typing import Any

from services.runtime_configuration import env_int


def _cluster_monitor_snapshot() -> dict[str, Any]:
    """Load the monitor lazily so callers and tests can replace the boundary."""

    from services.cluster_monitor_service import cluster_monitor_snapshot

    return cluster_monitor_snapshot()


def _nonnegative(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def configured_thresholds() -> tuple[int, int, int]:
    """Return active-task, waiting-task and retry thresholds.

    These are intentionally environment controls so operators can tune them
    without changing the settings API or touching account-pool data.
    """

    active_max = env_int("CHATGPT2API_MAINTENANCE_IMAGE_ACTIVE_MAX", 2, 0, 100000)
    waiting_max = env_int("CHATGPT2API_MAINTENANCE_IMAGE_WAITING_MAX", 0, 0, 100000)
    retry_seconds = env_int("CHATGPT2API_MAINTENANCE_RETRY_SECONDS", 30, 5, 3600)
    return active_max, waiting_max, retry_seconds


def cluster_image_load() -> dict[str, Any]:
    """Read aggregate image activity from the existing cluster monitor."""

    try:
        snapshot = _cluster_monitor_snapshot()
        threadpool = snapshot.get("threadpool") if isinstance(snapshot, dict) else {}
        image = threadpool.get("image") if isinstance(threadpool, dict) else {}
        active = _nonnegative(image.get("active")) if isinstance(image, dict) else 0
        waiting = _nonnegative(image.get("waiting")) if isinstance(image, dict) else 0
        limit = _nonnegative(image.get("limit")) if isinstance(image, dict) else 0
        active_max, waiting_max, retry_seconds = configured_thresholds()
        low_load = active <= active_max and waiting <= waiting_max
        return {
            "ok": True,
            "low_load": low_load,
            "image_active": active,
            "image_waiting": waiting,
            "image_limit": limit,
            "active_max": active_max,
            "waiting_max": waiting_max,
            "retry_seconds": retry_seconds,
            "source": "cluster_monitor",
        }
    except Exception as exc:
        # A missing monitor snapshot must fail closed for ordinary maintenance;
        # it is safer to retry later than compete with unknown image traffic.
        _, _, retry_seconds = configured_thresholds()
        return {
            "ok": False,
            "low_load": False,
            "image_active": None,
            "image_waiting": None,
            "image_limit": None,
            "active_max": configured_thresholds()[0],
            "waiting_max": configured_thresholds()[1],
            "retry_seconds": retry_seconds,
            "source": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
        }


def maintenance_is_allowed(*, emergency: bool = False) -> tuple[bool, dict[str, Any]]:
    """Return whether background maintenance may run now.

    Emergency replenishment is allowed when the confirmed account pool falls
    below its configured minimum.  This bypasses only the load gate; it does
    not change image concurrency or the account-pool coordination lock.
    """

    load = cluster_image_load()
    if emergency:
        load["bypassed"] = True
        load["bypass_reason"] = "account_pool_below_minimum"
        return True, load
    return bool(load.get("ok") and load.get("low_load")), load
