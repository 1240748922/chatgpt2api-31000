from threading import Event
from types import SimpleNamespace

import pytest

from services import account_maintenance as maintenance
from services import maintenance_load, cluster_monitor_service as monitor


def test_batch_stops_before_starting_more_work_when_traffic_rises(monkeypatch):
    load = iter([True, False])
    calls = []
    monkeypatch.setenv("CHATGPT2API_MAINTENANCE_BATCH_CONCURRENCY", "2")
    monkeypatch.setattr(maintenance, "maintenance_is_allowed", lambda: (next(load), {}))
    service = SimpleNamespace(sync_accounts_and_quota=lambda tokens: calls.append(tokens))
    assert maintenance.sync_idle_batch(service, list("abcdef"), Event()) == 2
    assert calls == [["a", "b"]]


def test_batch_does_not_start_after_shutdown(monkeypatch):
    stopped = Event()
    stopped.set()
    service = SimpleNamespace(sync_accounts_and_quota=lambda _: pytest.fail("started after shutdown"))
    assert maintenance.sync_idle_batch(service, ["a"], stopped) == 0


def test_batch_yields_after_time_slice(monkeypatch):
    elapsed = [0.0]
    calls = []
    def sync(tokens):
        calls.append(tokens)
        elapsed[0] += 21
    monkeypatch.setattr(maintenance, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    monkeypatch.setattr(maintenance, "maintenance_is_allowed", lambda: (True, {}))
    monkeypatch.setenv("CHATGPT2API_MAINTENANCE_BATCH_CONCURRENCY", "2")
    assert maintenance.sync_idle_batch(SimpleNamespace(sync_accounts_and_quota=sync), list("abcd"), Event()) == 2
    assert len(calls) == 1


def test_missing_replica_prevents_maintenance(monkeypatch):
    monkeypatch.setattr(maintenance_load, "_cluster_monitor_snapshot", lambda: {
        "cluster": {"expected": 8, "responding": 7},
        "threadpool": {"image": {"active": 0, "waiting": 0}},
    })
    assert maintenance_load.maintenance_is_allowed()[0] is False


def test_lightweight_cluster_counters_do_not_read_accounts_or_history(monkeypatch):
    monkeypatch.setattr(monitor, "account_shard_settings", lambda: (3, 1))
    monkeypatch.setattr(monitor, "_secret", lambda: "test-only")
    monkeypatch.setattr(monitor, "_local_image_load", lambda: {"threadpool": {"image": {"active": 1, "waiting": 0}}})
    monkeypatch.setattr(monitor.realtime_monitor_service, "snapshot", lambda: pytest.fail("full monitor snapshot"))
    def request(url, secret, timeout):
        assert url.endswith("/internal/monitor/load")
        assert timeout <= 2
        if "app0:" in url:
            return {"threadpool": {"image": {"active": 2, "waiting": 1}}}
        raise OSError("replica unavailable")
    monkeypatch.setattr(monitor, "_request_json", request)
    result = monitor.cluster_image_load_snapshot()
    assert result["cluster"] == {"expected": 3, "responding": 2}
    assert result["threadpool"]["image"]["active"] == 3
    assert result["threadpool"]["image"]["waiting"] == 1
