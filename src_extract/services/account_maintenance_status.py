"""Read the lifecycle owner's bounded telemetry, never another replica's zeros.

This is a read-only fallback for direct replica access or a stale gateway route.
It does not forward mutations, admin credentials, or scan the account pool.
"""
from __future__ import annotations

import json
import time
import urllib.request
from copy import deepcopy
from threading import Lock

from services.runtime_configuration import account_shard_settings

_lock = Lock()
_cache = None
_PATH = "/internal/monitor/account-maintenance"


def local_maintenance_view():
    from services.account_maintenance_policy import account_maintenance_policy
    from services.account_maintenance_progress import account_maintenance_progress
    from services.config import config

    _count, index = account_shard_settings()
    return {
        "maintenance": account_maintenance_policy.status(),
        "maintenance_progress": account_maintenance_progress.snapshot(owner=index == 0, instance=f"app{index}"),
        "cleanup_policy": {"auto_remove_invalid_accounts": config.auto_remove_invalid_accounts,
                           "renewal_failure_auto_delete": False},
    }


def local_internal_maintenance_view(secret):
    from services.cluster_monitor_service import _authorize
    _authorize(secret)
    # Never recurse/forward an internal call, even if DNS points at a non-owner.
    return local_maintenance_view()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _fetch_owner(secret):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request("http://app0:80" + _PATH,
                                     headers={"X-Cluster-Monitor-Secret": secret})
    with opener.open(request, timeout=2) as response:
        payload = json.loads(response.read(65537))
    progress = payload.get("maintenance_progress") if isinstance(payload, dict) else None
    if (not isinstance(progress, dict) or progress.get("owner") is not True
            or progress.get("instance") != "app0"
            or not isinstance(payload.get("maintenance"), dict)
            or not isinstance(payload.get("cleanup_policy"), dict)):
        raise ValueError("invalid maintenance owner response")
    return {key: payload[key] for key in ("maintenance", "maintenance_progress", "cleanup_policy")}


def _unavailable():
    from services.account_maintenance_progress import AccountMaintenanceProgress
    progress = AccountMaintenanceProgress().snapshot(instance="app0")
    progress["state"] = "unreachable"
    return {"maintenance": {"mode": "unavailable", "allowed": False, "batch_size": None,
                            "stale": True, "reasons": ["无法读取维护实例 app0 的进度；不代表后台已停止"]},
            "maintenance_progress": progress,
            "cleanup_policy": {"auto_remove_invalid_accounts": None, "renewal_failure_auto_delete": False}}


def maintenance_view():
    global _cache
    count, index = account_shard_settings()
    if index == 0:
        return local_maintenance_view()
    from services.cluster_monitor_service import _secret
    try:
        key = (count, index, _secret())
    except RuntimeError:
        return _unavailable()
    cached = _cache
    if cached and cached[0] == key and time.monotonic() < cached[1]:
        return deepcopy(cached[2])
    # One bounded fetch per replica, not one connection per open browser tab.
    if not _lock.acquire(blocking=False):
        return _unavailable()
    try:
        cached = _cache
        if cached and cached[0] == key and time.monotonic() < cached[1]:
            return deepcopy(cached[2])
        try:
            result, ttl = _fetch_owner(key[2]), 2
        except (OSError, ValueError, RuntimeError):
            result, ttl = _unavailable(), 1
        _cache = (key, time.monotonic() + ttl, deepcopy(result))
        return result
    finally:
        _lock.release()
