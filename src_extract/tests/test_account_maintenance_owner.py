import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services import account_maintenance_status as status


def owner_payload():
    return {"maintenance": {"mode": "normal", "batch_size": 2},
            "maintenance_progress": {"owner": True, "available": True, "instance": "app0",
                                     "active": 2, "sampled_at": time.time(),
                                     "totals": {"completed": 493, "succeeded": 1, "failed": 492}},
            "cleanup_policy": {"auto_remove_invalid_accounts": False, "renewal_failure_auto_delete": False}}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("CHATGPT2API_ACCOUNT_SHARD_COUNT", "8")
    monkeypatch.setenv("CHATGPT2API_ACCOUNT_SHARD_INDEX", "7")
    monkeypatch.setenv("CHATGPT2API_MONITOR_CLUSTER_SECRET", "synthetic-cluster-only")
    monkeypatch.setattr(status, "_cache", None)


def test_nonowner_reads_owner_not_local_nonowner(monkeypatch):
    calls = []
    monkeypatch.setattr(status, "_fetch_owner", lambda secret: calls.append(secret) or owner_payload())
    monkeypatch.setattr(status, "local_maintenance_view", lambda: pytest.fail("wrong replica telemetry"))
    first = status.maintenance_view()
    assert first["maintenance_progress"]["active"] == 2
    first["maintenance_progress"]["active"] = 999
    assert status.maintenance_view()["maintenance_progress"]["active"] == 2
    assert len(calls) == 1
    assert "synthetic-cluster" not in json.dumps(first)


def test_owner_never_uses_network(monkeypatch):
    monkeypatch.setenv("CHATGPT2API_ACCOUNT_SHARD_INDEX", "0")
    monkeypatch.setattr(status, "local_maintenance_view", owner_payload)
    monkeypatch.setattr(status, "_fetch_owner", lambda *_: pytest.fail("owner called itself"))
    assert status.maintenance_view()["maintenance_progress"]["instance"] == "app0"


def test_error_cache_is_short_and_recovers_without_restart(monkeypatch):
    clock = SimpleNamespace(now=100)
    monkeypatch.setattr(status, "time", SimpleNamespace(monotonic=lambda: clock.now))
    calls=[]
    def fetch(secret):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("must-not-leak-synthetic-secret")
        return owner_payload()
    monkeypatch.setattr(status, "_fetch_owner", fetch)
    failed = status.maintenance_view()
    assert failed["maintenance_progress"]["state"] == "unreachable"
    assert failed["maintenance_progress"]["active"] is None
    assert "must-not-leak" not in json.dumps(failed)
    assert status.maintenance_view() == failed
    clock.now += 1.01
    assert status.maintenance_view()["maintenance_progress"]["active"] == 2
    assert len(calls) == 2


def test_parallel_detail_polling_does_not_fan_out(monkeypatch):
    entered, release = Event(), Event()
    calls=[]
    def fetch(secret):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return owner_payload()
    monkeypatch.setattr(status, "_fetch_owner", fetch)
    with ThreadPoolExecutor(1) as executor:
        first=executor.submit(status.maintenance_view)
        try:
            assert entered.wait(2)
            assert status.maintenance_view()["maintenance_progress"]["active"] is None
        finally:
            release.set()
        assert first.result(timeout=2)["maintenance_progress"]["active"] == 2
    assert len(calls) == 1


@pytest.mark.parametrize("payload", [{}, {"maintenance_progress": {"owner": False, "instance": "app7"}}])
def test_dns_misdirection_is_not_accepted_as_owner(monkeypatch, payload):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, limit):
            assert limit == 65537
            return json.dumps(payload).encode()
    def opener(*handlers):
        assert any(isinstance(h, status._NoRedirect) for h in handlers)
        assert any(isinstance(h, status.urllib.request.ProxyHandler) and not h.proxies for h in handlers)
        def open_(request, timeout):
            assert request.full_url == "http://app0:80/internal/monitor/account-maintenance"
            assert request.get_header("Authorization") is None
            assert timeout == 2
            return Response()
        return SimpleNamespace(open=open_)
    monkeypatch.setattr(status.urllib.request, "build_opener", opener)
    assert status.maintenance_view()["maintenance_progress"]["state"] == "unreachable"


def test_new_internal_endpoint_requires_secret_and_never_recurses(monkeypatch):
    from api import system
    monkeypatch.setattr(status, "_fetch_owner", lambda *_: pytest.fail("internal endpoint recursed"))
    app=FastAPI()
    app.include_router(system.create_router("test-version"))
    with TestClient(app) as client:
        path="/internal/monitor/account-maintenance"
        assert client.get(path).status_code == 403
        assert client.get(path, headers={"X-Cluster-Monitor-Secret": "wrong"}).status_code == 403
        data=client.get(path, headers={"X-Cluster-Monitor-Secret": "synthetic-cluster-only"}).json()
        assert data["maintenance_progress"]["owner"] is False
        assert data["maintenance_progress"]["instance"] == "app7"


def test_admin_only_and_availability_share_authoritative_owner(monkeypatch):
    from api import accounts
    calls=[]
    monkeypatch.setattr(status, "_fetch_owner", lambda *_: calls.append(1) or owner_payload())
    monkeypatch.setattr(accounts.account_service, "readiness_summary", lambda: {"total": 123})
    app=FastAPI()
    app.include_router(accounts.create_router())
    with TestClient(app) as client:
        assert client.get("/api/accounts/maintenance-status").status_code == 401
        assert not calls
        headers={"Authorization": "Bearer test-only"}
        for path in ("/api/accounts/maintenance-status", "/api/accounts/availability"):
            data=client.get(path, headers=headers).json()
            assert data["maintenance_progress"]["totals"]["completed"] == 493
            assert data["maintenance_progress"]["instance"] == "app0"
        assert len(calls) == 1
