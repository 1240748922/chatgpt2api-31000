from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from threading import Lock
from typing import Any, Mapping

from services.realtime_monitor_service import realtime_monitor_service
from services.runtime_configuration import account_shard_settings


_INTERNAL_PATH = "/internal/monitor/realtime"
_OPERATIONS_TTL = 1.0
_operations_lock = Lock()
_operations_cache: tuple[tuple[int, int, str], float, dict[str, Any]] | None = None
_load_lock = Lock()
_load_cache = None


def _int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _secret() -> str:
    value = str(os.getenv("CHATGPT2API_MONITOR_CLUSTER_SECRET") or "").strip()
    if not value:
        raise RuntimeError("CHATGPT2API_MONITOR_CLUSTER_SECRET is required")
    return value


def _authorize(provided: str) -> None:
    import secrets

    expected = _secret()
    if not provided or not secrets.compare_digest(provided, expected):
        raise PermissionError("invalid cluster monitor secret")


def local_internal_snapshot(provided: str) -> dict[str, Any]:
    _authorize(provided)
    return realtime_monitor_service.snapshot()


def _local_image_load() -> dict[str, Any]:
    from services.log_service import image_threadpool_snapshot
    from services.maintenance_pressure import local_pressure_snapshot

    try:
        health = local_pressure_snapshot()
    except Exception:
        # A failed diagnostic must not break the existing load counter API.
        # The performance policy treats schema 0 as incomplete, not idle.
        health = {"schema": 0}
    return {"threadpool": {"image": image_threadpool_snapshot()}, "maintenance_health": health}


def local_internal_image_load(provided: str) -> dict[str, Any]:
    _authorize(provided)
    return _local_image_load()


def local_internal_operations(provided: str) -> dict[str, int]:
    _authorize(provided)
    _, index = account_shard_settings()
    return {"instance_index": index, **realtime_monitor_service.operations_snapshot()}


def _collect_operations(count: int, own_index: int, secret: str) -> dict[str, Any]:
    # Never use snapshot()["active"] here: it scans history/accounts and its
    # displayed record list is capped. We need the full O(1) active counter.
    active = realtime_monitor_service.operations_snapshot()["active_requests"]
    responding = 1
    if count > 1 and secret:
        with ThreadPoolExecutor(max_workers=min(count - 1, 16), thread_name_prefix="dashboard-count") as executor:
            futures = {
                executor.submit(_request_json, f"http://app{index}:80/internal/monitor/operations", secret, 1.0): index
                for index in range(count) if index != own_index
            }
            for future in as_completed(futures):
                try:
                    payload = future.result()
                    value, index = payload.get("active_requests"), payload.get("instance_index")
                    if type(value) is not int or value < 0 or type(index) is not int or index != futures[future]:
                        continue
                    active += value
                    responding += 1
                except (OSError, ValueError, RuntimeError, urllib.error.URLError):
                    # Missing/old replicas are explicitly partial, not zeros.
                    continue
    return {
        "active_requests": active,
        "scope": "cluster" if count > 1 else "instance",
        "expected_instances": count,
        "responding_instances": responding,
        "complete": responding == count,
    }


def cluster_operations_snapshot() -> dict[str, Any]:
    """Read-only dashboard sample; coalesce callers without touching admission.

    One-second cache and single flight keep multiple dashboards from fanning
    out independently. The lock is separate from the request lifecycle lock.
    No database, account pool, durable counter or background task is involved.
    """
    global _operations_cache
    count, own_index = account_shard_settings()
    if count <= 1:
        return _collect_operations(1, own_index, "")
    try:
        secret = _secret()
    except RuntimeError:
        secret = ""
    key = (count, own_index, secret)
    with _operations_lock:
        if _operations_cache is not None:
            cached_key, expires, result = _operations_cache
            if cached_key == key and time.monotonic() < expires:
                return dict(result)
        result = _collect_operations(count, own_index, secret)
        _operations_cache = (key, time.monotonic() + _OPERATIONS_TTL, result)
        return dict(result)


def cluster_image_load_snapshot() -> dict[str, Any]:
    """Coalesce maintenance polling; never called on image dispatch's hot path."""
    global _load_cache
    key = (*account_shard_settings(), os.getenv("CHATGPT2API_MONITOR_CLUSTER_SECRET", ""),
           os.getenv("CHATGPT2API_MAINTENANCE_IMPORTER_URL", ""))
    with _load_lock:
        if _load_cache is not None and _load_cache[0] == key and time.monotonic() - _load_cache[1] < 2:
            return deepcopy(_load_cache[2])
        snapshot = _collect_image_load_snapshot()
        _load_cache = (key, time.monotonic(), snapshot)
        return deepcopy(snapshot)


def _collect_image_load_snapshot() -> dict[str, Any]:
    """Read only admission counters; no accounts, logs or history are scanned."""
    count, own_index = account_shard_settings()
    snapshots = [_local_image_load()]
    if count > 1:
        secret = _secret()
        with ThreadPoolExecutor(max_workers=min(count - 1, 16), thread_name_prefix="maintenance-load") as executor:
            futures = [
                executor.submit(_request_json, f"http://app{index}:80/internal/monitor/load", secret, 2)
                for index in range(count) if index != own_index
            ]
            for future in as_completed(futures):
                try:
                    snapshot = future.result()
                    counters = (snapshot.get("threadpool") or {}).get("image")
                    if isinstance(counters, dict) and {"active", "waiting"} <= counters.keys():
                        snapshots.append(snapshot)
                except (OSError, ValueError, RuntimeError, urllib.error.URLError):
                    continue
    reports = [snapshot.get("maintenance_health") for snapshot in snapshots]
    importer = os.getenv("CHATGPT2API_MAINTENANCE_IMPORTER_URL", "").strip().rstrip("/")
    if importer:
        try:
            reports.append(_request_json(importer + "/internal/monitor/load", _secret(), 2).get("maintenance_health"))
        except (OSError, ValueError, RuntimeError, urllib.error.URLError):
            pass
    return {
        "cluster": {"expected": count, "responding": len(snapshots)},
        "maintenance_cluster": {"expected": count + int(bool(importer)), "responding": len(reports)},
        "threadpool": {"image": _pool_sum(snapshots, "image")},
        "maintenance_health": reports,
    }


def local_internal_call_detail(call_id: str, provided: str) -> dict[str, Any] | None:
    _authorize(provided)
    return realtime_monitor_service.call_detail(call_id)


def _request_json(url: str, secret: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"X-Cluster-Monitor-Secret": secret})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise RuntimeError("invalid monitor payload")
    return payload


def _fetch_snapshot(index: int, secret: str) -> dict[str, Any]:
    return _request_json(f"http://app{index}:80{_INTERNAL_PATH}", secret, 8)


def cluster_monitor_snapshot() -> dict[str, Any]:
    count, _index = account_shard_settings()
    if count <= 1:
        return realtime_monitor_service.snapshot()
    secret = _secret()
    snapshots: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=count, thread_name_prefix="cluster-monitor") as executor:
        futures = [executor.submit(_fetch_snapshot, index, secret) for index in range(count)]
        for future in as_completed(futures):
            try:
                snapshots.append(future.result())
            except (OSError, ValueError, RuntimeError, urllib.error.URLError):
                continue
    if not snapshots:
        local = realtime_monitor_service.snapshot()
        return {**local, "cluster": {"expected": count, "responding": 0}}
    return _merge_snapshots(snapshots, count)


def cluster_monitor_call_detail(call_id: str) -> dict[str, Any] | None:
    normalized = str(call_id or "").strip()
    if not normalized:
        return None
    local = realtime_monitor_service.call_detail(normalized)
    if local is not None:
        return local
    count, index = account_shard_settings()
    if count <= 1:
        return None
    secret = _secret()

    def fetch(shard: int) -> dict[str, Any] | None:
        try:
            return _request_json(
                f"http://app{shard}:80{_INTERNAL_PATH}/{normalized}",
                secret,
                5,
            )
        except (OSError, ValueError, RuntimeError, urllib.error.URLError):
            return None

    with ThreadPoolExecutor(max_workers=max(1, count - 1), thread_name_prefix="cluster-monitor-detail") as executor:
        futures = [executor.submit(fetch, shard) for shard in range(count) if shard != index]
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                return result
    return None


def _pool_sum(snapshots: list[Mapping[str, Any]], key: str) -> dict[str, int]:
    fields = ("limit", "active", "waiting", "queue_limit", "admitted", "rejected")
    return {
        field: sum(
            _int(((snapshot.get("threadpool") or {}).get(key) or {}).get(field))
            for snapshot in snapshots
        )
        for field in fields
    }


def _merge_snapshots(snapshots: list[Mapping[str, Any]], expected_count: int) -> dict[str, Any]:
    active = [item for snapshot in snapshots for item in snapshot.get("active", []) if isinstance(item, dict)]
    recent = [item for snapshot in snapshots for item in snapshot.get("recent", []) if isinstance(item, dict)]
    slow = [item for snapshot in snapshots for item in snapshot.get("slow", []) if isinstance(item, dict)]
    events = [item for snapshot in snapshots for item in snapshot.get("events", []) if isinstance(item, dict)]
    active.sort(key=lambda item: _int(item.get("elapsed_ms")), reverse=True)
    recent.sort(key=lambda item: str(item.get("ended_at") or item.get("updated_at") or ""), reverse=True)
    slow.sort(key=lambda item: _int(item.get("duration_ms")), reverse=True)
    events.sort(key=lambda item: str(item.get("time") or ""), reverse=True)

    threadpool = {
        "tokens": sum(_int((snapshot.get("threadpool") or {}).get("tokens")) for snapshot in snapshots),
        "previous_tokens": sum(_int((snapshot.get("threadpool") or {}).get("previous_tokens")) for snapshot in snapshots),
        "runtime_threads": sum(_int((snapshot.get("threadpool") or {}).get("runtime_threads")) for snapshot in snapshots),
        "account_processing": _pool_sum(snapshots, "account_processing"),
        "image": _pool_sum(snapshots, "image"),
        "upscale": _pool_sum(snapshots, "upscale"),
    }
    shards = [((snapshot.get("threadpool") or {}).get("account_shard") or {}) for snapshot in snapshots]
    threadpool["account_shard"] = {
        "count": expected_count,
        "index": 0,
        "assigned_accounts": sum(_int(shard.get("assigned_accounts")) for shard in shards),
        "ready_accounts": sum(_int(shard.get("ready_accounts")) for shard in shards),
        "inflight": sum(_int(shard.get("inflight")) for shard in shards),
    }
    return {
        "cluster": {"expected": expected_count, "responding": len(snapshots)},
        "updated_at": max((str(snapshot.get("updated_at") or "") for snapshot in snapshots), default=""),
        "threadpool": threadpool,
        "window": {
            "completed": sum(_int((snapshot.get("window") or {}).get("completed")) for snapshot in snapshots),
            "completed_capacity": sum(_int((snapshot.get("window") or {}).get("completed_capacity")) for snapshot in snapshots),
            "events": sum(_int((snapshot.get("window") or {}).get("events")) for snapshot in snapshots),
            "event_capacity": sum(_int((snapshot.get("window") or {}).get("event_capacity")) for snapshot in snapshots),
        },
        "summary": realtime_monitor_service.summary_for_records(active, recent),
        "active": active[:100],
        "recent": recent[:80],
        "slow": slow[:50],
        "events": events[:80],
        "metric_labels": next((dict(snapshot.get("metric_labels") or {}) for snapshot in snapshots), {}),
    }
