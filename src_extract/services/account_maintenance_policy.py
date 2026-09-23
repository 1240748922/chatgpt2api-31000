"""Performance-aware gate for account maintenance, not retention or registration."""
from __future__ import annotations

import math
import os
import time
from copy import deepcopy
from threading import Lock

from services.runtime_configuration import env_int


REASONS = {
    "healthy": "性能正常，允许小批同步",
    "warming": "性能样本不足，单账号试探",
    "partial": "部分实例指标缺失，暂停新维护批次",
    "cpu": "CPU 使用率较高", "memory": "内存压力较高",
    "database_pool": "数据库连接池接近饱和",
    "database_ms": "数据库操作耗时较高", "database_error": "数据库错误率较高",
    "writer_wait_ms": "账号写锁等待较长", "queue_ms": "请求入口排队较长",
    "upstream_ms": "账号上游接口响应较慢", "upstream_error": "账号上游接口错误率较高",
    "recovering": "压力回落，恢复观察中", "unavailable": "无法取得集群性能指标",
    "legacy": "已选择旧版低并发维护策略", "configuration": "账号维护策略配置无效",
}


def _number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _sample(samples, metric):
    sample = samples.get(metric)
    if not isinstance(sample, dict):
        return {}
    count, age = sample.get("count"), sample.get("age_seconds")
    if type(count) is not int or count < 3 or not _number(age) or age > 60:
        return {}
    return sample


def evaluate_pressure(snapshot, *, now=None):
    """Worst replica wins; a slow image model alone is never a pressure signal."""
    now = time.time() if now is None else now
    if not isinstance(snapshot, dict):
        return "paused", ["partial"]
    cluster = snapshot.get("maintenance_cluster") or snapshot.get("cluster") or {}
    if not isinstance(cluster, dict):
        return "paused", ["partial"]
    expected, responding = cluster.get("expected", 1), cluster.get("responding", 1)
    reports = snapshot.get("maintenance_health") or []
    if type(expected) is not int or expected < 1 or responding != expected or not isinstance(reports, list) or len(reports) != expected:
        return "paused", ["partial"]
    limits = {
        "database_ms": env_int("CHATGPT2API_MAINTENANCE_DB_P95_MS", 500),
        "writer_wait_ms": env_int("CHATGPT2API_MAINTENANCE_WRITE_WAIT_P95_MS", 500),
        "queue_ms": env_int("CHATGPT2API_MAINTENANCE_QUEUE_P95_MS", 1000),
        "upstream_ms": env_int("CHATGPT2API_MAINTENANCE_UPSTREAM_P95_MS", 10000),
    }
    warnings, critical = set(), set()
    resources_known, has_samples = True, False
    for report in reports:
        if not isinstance(report, dict) or report.get("schema") != 1 or not _number(report.get("sampled_at")) or not -5 <= now - report["sampled_at"] <= 15:
            return "paused", ["partial"]
        for key, soft, hard in [("cpu_percent", 75, 95), ("memory_percent", 80, 95), ("database_pool_utilization", .8, 1)]:
            value = report.get(key)
            if not _number(value):
                if key != "database_pool_utilization":
                    resources_known = False
                continue
            reason = {"cpu_percent": "cpu", "memory_percent": "memory", "database_pool_utilization": "database_pool"}[key]
            if value >= soft:
                warnings.add(reason)
            if value >= hard:
                critical.add(reason)
        samples = report.get("samples") or {}
        if not isinstance(samples, dict):
            return "paused", ["partial"]
        for metric, threshold in limits.items():
            value = _sample(samples, metric).get("p95")
            if not _number(value):
                continue
            if value >= threshold:
                warnings.add(metric)
            if value >= threshold * 2 and metric != "queue_ms":
                critical.add(metric)
        for metric in ["upstream_error", "database_error"]:
            sample = _sample(samples, metric)
            rate = sample.get("mean")
            if _number(rate):
                if rate >= .2:
                    warnings.add(metric)
                if rate >= .5:
                    critical.add(metric)
        # Cold start can probe; absence of traffic is not permanently unhealthy.
        # Idle replicas naturally have no new SQL/OAuth samples. Requiring
        # traffic on every replica would leave a quiet cluster probing one
        # account forever. Every replica still contributes resource pressure.
        has_samples = has_samples or any(_sample(samples, metric) for metric in (*limits, "upstream_error", "database_error"))
    if critical:
        return "paused", sorted(critical)
    if warnings:
        return "reduced", sorted(warnings)
    return ("normal", ["healthy"]) if resources_known and has_samples else ("probe", ["warming"])


class AccountMaintenancePolicy:
    def __init__(self):
        self._lock = Lock()
        self._hold_until = 0.0
        self._last = {"mode": "not_sampled", "allowed": False, "batch_size": 0,
                      "reasons": ["等待首次性能采样"], "policy": "performance", "sampled_at": None}

    def decide(self, snapshot, *, now=None, monotonic=None):
        now = time.time() if now is None else now
        monotonic = time.monotonic() if monotonic is None else monotonic
        mode, codes = evaluate_pressure(snapshot, now=now)
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        with self._lock:
            if mode == "paused":
                self._hold_until = monotonic + 10
            elif monotonic < self._hold_until:
                mode, codes = "paused", ["recovering"]
            width = env_int("CHATGPT2API_MAINTENANCE_BATCH_CONCURRENCY", 2, 1, 8) if mode == "normal" else (0 if mode == "paused" else 1)
            self._last = {"mode": mode, "allowed": width > 0, "batch_size": width,
                          "policy": "performance", "sampled_at": now, "reason_codes": codes,
                          "reasons": [REASONS[code] for code in codes],
                          "active_images": ((snapshot.get("threadpool") or {}).get("image") or {}).get("active"),
                          "hold_seconds": max(0, round(self._hold_until - monotonic)),
                          "cluster": snapshot.get("cluster") or {}}
            reports = snapshot.get("maintenance_health")
            reports = [r for r in reports if isinstance(r, dict)] if isinstance(reports, list) else []
            signals = {}
            for key in ("cpu_percent", "memory_percent", "database_pool_utilization"):
                values = [r[key] for r in reports if _number(r.get(key))]
                signals[key] = max(values) if values else None
            for key in ("database_ms", "writer_wait_ms", "queue_ms", "upstream_ms", "upstream_error"):
                field = "mean" if key.endswith("error") else "p95"
                values = [_sample(r.get("samples") or {}, key).get(field) for r in reports
                          if isinstance(r.get("samples"), dict)]
                values = [value for value in values if _number(value)]
                signals[key] = max(values) if values else None
            self._last["signals"] = signals
            return deepcopy(self._last)

    def status(self):
        with self._lock:
            result = deepcopy(self._last)
        sampled = result.get("sampled_at")
        result["stale"] = sampled is not None and time.time() - sampled > max(
            90, env_int("CHATGPT2API_MAINTENANCE_RETRY_SECONDS", 30, 5, 3600) * 3)
        return result

    def compatibility_decision(self, *, invalid=False):
        from services.maintenance_load import maintenance_is_allowed
        allowed, load = (False, {}) if invalid else maintenance_is_allowed()
        with self._lock:
            self._hold_until = 0
            reason = "configuration" if invalid else "legacy"
            self._last = {"mode": "normal" if allowed else "paused", "allowed": allowed,
                          "batch_size": env_int("CHATGPT2API_MAINTENANCE_BATCH_CONCURRENCY", 2, 1, 8) if allowed else 0,
                          "policy": "invalid" if invalid else "idle", "sampled_at": time.time(),
                          "reasons": [REASONS[reason]], "reason_codes": [reason],
                          "active_images": load.get("image_active"), "signals": {}}
            return deepcopy(self._last)


account_maintenance_policy = AccountMaintenancePolicy()


def account_maintenance_decision():
    policy = os.getenv("CHATGPT2API_ACCOUNT_MAINTENANCE_POLICY", "performance").strip().lower()
    if policy != "performance":
        return account_maintenance_policy.compatibility_decision(invalid=policy != "idle")
    from services.cluster_monitor_service import cluster_image_load_snapshot
    try:
        snapshot = cluster_image_load_snapshot()
    except Exception:
        snapshot = {"cluster": {"expected": 1, "responding": 0}}
    return account_maintenance_policy.decide(snapshot)
