"""Synthetic eight-instance regression; never contacts real upstreams/accounts."""
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

import pytest

from services import cluster_monitor_service as cluster
from services import dashboard_view as dashboard
from services.realtime_monitor_service import RealtimeMonitorService


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    monkeypatch.setattr(cluster, "_operations_cache", None)


@pytest.fixture
def small_cluster(monkeypatch):
    monitor = RealtimeMonitorService()
    monitor.start("local", endpoint="/v1/images/edits", model="test")
    monkeypatch.setattr(cluster, "realtime_monitor_service", monitor)
    monkeypatch.setattr(cluster, "account_shard_settings", lambda: (3, 1))
    monkeypatch.setattr(cluster, "_secret", lambda: "synthetic-only")
    monkeypatch.setattr(monitor, "snapshot", lambda: pytest.fail("must not scan full monitor"))
    return monitor


def stub_dashboard(monkeypatch):
    monkeypatch.setattr(dashboard, "account_service", SimpleNamespace(get_stats=lambda: {"active": 1}))
    monkeypatch.setattr(dashboard, "dashboard_metrics_service", SimpleNamespace(
        snapshot_many=lambda: {"metrics": {"ready": True}, "ranges": {}}))
    monkeypatch.setattr(dashboard, "config", SimpleNamespace(
        get_storage_backend=lambda: SimpleNamespace(get_backend_info=lambda: {}),
        get_image_storage_settings=lambda: {}))
    monkeypatch.setattr(dashboard, "runtime_environment_snapshot", lambda: {})


def test_dashboard_counts_all_eight_instances_not_just_app0(monkeypatch):
    counts = [1, 125, 20, 0, 3, 40, 8, 2]
    instances = [RealtimeMonitorService() for _ in counts]
    for index, count in enumerate(counts):
        for item in range(count):
            instances[index].start(f"{index}-{item}", endpoint="/v1/images/generations", model="test")
    monkeypatch.setattr(cluster, "account_shard_settings", lambda: (8, 0))
    monkeypatch.setattr(cluster, "_secret", lambda: "synthetic-only")
    monkeypatch.setattr(cluster, "realtime_monitor_service", instances[0])
    # Before the fix dashboard imports and counts only this local instance.
    if hasattr(dashboard, "realtime_monitor_service"):
        monkeypatch.setattr(dashboard, "realtime_monitor_service", instances[0])

    def request(url, secret, timeout):
        assert url.endswith("/internal/monitor/operations")
        assert secret == "synthetic-only"
        assert timeout <= 1
        index = int(url.split("app")[1].split(":")[0])
        assert index != 0
        return {"instance_index": index, **instances[index].operations_snapshot()}

    monkeypatch.setattr(cluster, "_request_json", request)
    stub_dashboard(monkeypatch)
    operations = dashboard.build_dashboard_view(app_version="test")["operations"]
    assert operations["active_requests"] == sum(counts)
    assert operations == dict(active_requests=199, expected_instances=8, responding_instances=8, scope="cluster", complete=True)


@pytest.mark.parametrize("bad", [None, {}, {"active_requests": -1}, {"active_requests": True},
                                {"active_requests": "10"}, {"active_requests": 5, "instance_index": 1}])
def test_missing_old_or_invalid_node_is_marked_partial(monkeypatch, small_cluster, bad):
    def request(url, *_):
        if "app0:" in url:
            return dict(active_requests=125, instance_index=0)
        if bad is None:
            raise OSError("node unavailable")
        return {"instance_index": 2, **bad}
    monkeypatch.setattr(cluster, "_request_json", request)
    result = cluster.cluster_operations_snapshot()
    assert result == dict(active_requests=126, expected_instances=3, responding_instances=2, scope="cluster", complete=False)


def test_no_secret_is_partial_without_network(monkeypatch, small_cluster):
    def missing():
        raise RuntimeError("missing secret")
    monkeypatch.setattr(cluster, "_secret", missing)
    monkeypatch.setattr(cluster, "_request_json", lambda *_: pytest.fail("no secret must not send requests"))
    result = cluster.cluster_operations_snapshot()
    assert result["active_requests"] == 1
    assert result["responding_instances"] == 1 and not result["complete"]


def test_single_instance_lifecycle_needs_neither_secret_nor_network(monkeypatch, small_cluster):
    monkeypatch.setattr(cluster, "account_shard_settings", lambda: (1, 0))
    monkeypatch.setattr(cluster, "_secret", lambda: pytest.fail("no secret needed"))
    monkeypatch.setattr(cluster, "_request_json", lambda *_: pytest.fail("local deployment"))
    assert cluster.cluster_operations_snapshot() == dict(
        active_requests=1, scope="instance", expected_instances=1, responding_instances=1, complete=True)
    small_cluster.finish(dict(call_id="local", status="success"))
    assert cluster.cluster_operations_snapshot()["active_requests"] == 0


def test_cache_refreshes_and_finished_requests_disappear(monkeypatch, small_cluster):
    clock, calls, remote = [100.0], [], [125]
    monkeypatch.setattr(cluster, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def request(url, *_):
        calls.append(url)
        index = 0 if "app0:" in url else 2
        return dict(active_requests=remote[0], instance_index=index)
    monkeypatch.setattr(cluster, "_request_json", request)
    first = cluster.cluster_operations_snapshot()
    assert first["active_requests"] == 251
    first["active_requests"] = -99  # callers cannot corrupt the cache
    small_cluster.finish(dict(call_id="local", status="failed"))
    remote[0] = 0
    assert cluster.cluster_operations_snapshot()["active_requests"] == 251
    assert len(calls) == 2
    clock[0] += 1.01
    assert cluster.cluster_operations_snapshot()["active_requests"] == 0
    assert len(calls) == 4
    # A shard topology change must not reuse a sample from the previous layout.
    monkeypatch.setattr(cluster, "account_shard_settings", lambda: (2, 1))
    assert cluster.cluster_operations_snapshot()["expected_instances"] == 2
    assert len(calls) == 5


def test_many_dashboard_readers_share_one_parallel_fanout(monkeypatch, small_cluster):
    readers, peers = Barrier(12), Barrier(2)
    release, entered = Event(), Event()
    calls, lock = [], Lock()
    def request(url, *_):
        with lock:
            calls.append(url)
        # Both peers must run concurrently rather than pay serial timeouts.
        peers.wait(timeout=5)
        entered.set()
        assert release.wait(timeout=5)
        return dict(active_requests=10, instance_index=0 if "app0:" in url else 2)
    monkeypatch.setattr(cluster, "_request_json", request)
    def read():
        readers.wait(timeout=5)
        return cluster.cluster_operations_snapshot()
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(read) for _ in range(12)]
        assert entered.wait(timeout=5)
        # Remote IO must not hold the request lifecycle lock.
        small_cluster.start("during-sample", endpoint="/v1/images/edits", model="test")
        small_cluster.finish(dict(call_id="during-sample", status="success"))
        release.set()
        results = [future.result(timeout=5) for future in futures]
    assert len(calls) == 2
    assert all(result["active_requests"] == 21 for result in results)


def test_internal_counter_route_authorization_and_payload(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api import system

    monitor = RealtimeMonitorService()
    for i in range(140):
        monitor.start(str(i), endpoint="/v1/images/edits", model="test")
    monkeypatch.setattr(cluster, "realtime_monitor_service", monitor)
    monkeypatch.setattr(cluster, "account_shard_settings", lambda: (8, 4))
    monkeypatch.setenv("CHATGPT2API_MONITOR_CLUSTER_SECRET", "synthetic-only")
    monkeypatch.setattr(monitor, "snapshot", lambda: pytest.fail("full monitor scan"))
    app = FastAPI()
    app.include_router(system.create_router("test"))
    with TestClient(app) as client:
        path = "/internal/monitor/operations"
        assert client.get(path).status_code == 403
        assert client.get(path, headers={"X-Cluster-Monitor-Secret": "wrong"}).status_code == 403
        response = client.get(path, headers={"X-Cluster-Monitor-Secret": "synthetic-only"})
        assert response.status_code == 200
        assert response.json() == dict(active_requests=140, instance_index=4)
        monkeypatch.delenv("CHATGPT2API_MONITOR_CLUSTER_SECRET")
        assert client.get(path).status_code == 403


def test_operations_metadata_survives_dashboard_response_serialization():
    from api.dashboard_contract import DashboardResponseView
    from dashboard_fixtures import dashboard_payload
    payload = dashboard_payload()
    result = DashboardResponseView.model_validate(payload).model_dump()
    assert result["operations"] == payload["operations"]
