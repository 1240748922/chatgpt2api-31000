import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services import account_maintenance as maintenance
from services import account_maintenance_progress as module
from services.account_service import OAuthRefreshError
from test_account_auth_quarantine import jwt, row, service_factory


@pytest.fixture
def progress(monkeypatch):
    tracker = module.AccountMaintenanceProgress()
    monkeypatch.setattr(module, "account_maintenance_progress", tracker)
    monkeypatch.setattr(maintenance, "account_maintenance_progress", tracker)
    monkeypatch.setattr(maintenance, "account_maintenance_decision", lambda: {
        "allowed": True, "batch_size": 2, "mode": "normal", "reason_codes": []})
    tracker.started()
    return tracker


def wait_active(progress, expected):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if progress.snapshot()["active"] == expected:
            return
        time.sleep(.01)
    pytest.fail(f"active did not become {expected}: {progress.snapshot()}")


@pytest.mark.parametrize("kind", ["renewal", "quota"])
def test_actual_workers_report_inflight_and_results(progress, service_factory, monkeypatch, kind):
    monkeypatch.setenv("CHATGPT2API_STRICT_IMAGE_CREDENTIALS", "1")
    first, second, fresh = jwt(-3000), jwt(-3001), jwt(7200)
    service = service_factory([row(first, refresh_token="private-rt-0"), row(second, refresh_token="private-rt-1")])
    release = Event()

    def exchange(rt, account, **kw):
        assert release.wait(5)
        if rt.endswith("1"):
            raise OAuthRefreshError(401, "refresh_token_reused", "synthetic")
        return {"access_token": fresh, "refresh_token": "private-new-rt"}

    def sync(token, *a, **kw):
        assert release.wait(5)
        if token == second:
            raise TimeoutError("do-not-leak-this-credential")
        return service.get_account(token)

    monkeypatch.setattr(service, "_request_access_token_refresh", exchange)
    monkeypatch.setattr(service, "fetch_remote_info", sync)
    operation = maintenance.renew_idle_batch if kind == "renewal" else maintenance.sync_idle_batch
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(operation, service, [first, second], Event())
        try:
            wait_active(progress, 2)
            live = progress.snapshot()
            assert live["batch"]["kind"] == kind
            assert live["batch"]["total"] == 2 and live["batch"]["completed"] == 0
            assert live["batch"]["queued"] == 0
        finally:
            release.set()
        assert future.result(timeout=5) == 2
    state = progress.snapshot()
    assert state["active"] == 0 and state["batch"] is None
    assert state["last_batch"]["completed"] == 2
    assert state["totals"] == dict(completed=2, succeeded=1, failed=1, skipped=0)
    text = json.dumps(state)
    assert "private" not in text and "credential" not in text and first not in text


def test_manual_operations_do_not_count_as_background(progress, service_factory, monkeypatch):
    token = jwt(7 * 86400)
    service = service_factory([row(token, refresh_token="private-rt")])
    monkeypatch.setattr(service, "fetch_remote_info", lambda *a, **kw: service.get_account(token))
    service.sync_accounts_and_quota([token])
    service.renew_expiring_access_tokens([token])
    assert progress.snapshot()["totals"]["completed"] == 0
    assert module.current_maintenance_batch() is None


def test_preflight_skips_and_missing_accounts_are_counted(progress, service_factory):
    token = jwt(7 * 86400)
    service = service_factory([row(token, refresh_token="private-rt")])
    maintenance.renew_idle_batch(service, [token, "missing-synthetic-account"], Event())
    assert progress.snapshot()["totals"] == dict(completed=2, succeeded=0, failed=1, skipped=1)


def test_batch_exception_clears_current_and_preserves_context(progress):
    with pytest.raises(RuntimeError):
        with progress.batch("quota", 2) as batch:
            batch.call(lambda: {}, lambda _: "succeeded")
            raise RuntimeError("private-must-not-be-recorded")
    state = progress.snapshot()
    assert state["batch"] is None and state["active"] == 0
    assert state["last_batch"]["status"] == "interrupted"
    assert state["totals"]["completed"] == 1
    assert "private" not in json.dumps(state)
    assert module.current_maintenance_batch() is None


def test_unknown_and_nonowner_are_not_reported_as_zero(progress):
    cold = module.AccountMaintenanceProgress().snapshot()
    assert not cold["available"] and cold["active"] is None and cold["totals"] is None
    other = progress.snapshot(owner=False, instance="app3")
    assert other["state"] == "not_owner" and other["active"] is None
    assert not other["available"] and other["totals"] is None
    progress.waiting(30)
    assert progress.snapshot()["state"] == "waiting"
    assert progress.snapshot()["next_check_at"] > time.time()
    progress.stopped()
    assert progress.snapshot()["state"] == "stopped"
    assert progress.snapshot()["next_check_at"] is None


def test_snapshot_is_detached_and_storage_stays_bounded(progress):
    for _ in range(100):
        with progress.batch("quota", 1) as batch:
            batch.call(lambda: None, lambda _: "skipped")
    data = progress.snapshot()
    data["totals"]["completed"] = -1
    data["last_batch"]["total"] = -1
    assert progress.snapshot()["totals"]["completed"] == 100
    assert progress.snapshot()["last_batch"]["total"] == 1
    assert len(json.dumps(progress.snapshot())) < 1500


def test_lightweight_admin_api_never_scans_pool_or_exposes_config(progress, monkeypatch):
    from api import accounts
    monkeypatch.setenv("CHATGPT2API_ACCOUNT_SHARD_INDEX", "0")
    monkeypatch.setattr(accounts.account_service, "readiness_summary", lambda: pytest.fail("full pool scan"))
    monkeypatch.setitem(accounts.config.data, "auto_remove_invalid_accounts", False)
    app = FastAPI()
    app.include_router(accounts.create_router())
    client = TestClient(app)
    assert client.get("/api/accounts/maintenance-status").status_code == 401
    response = client.get("/api/accounts/maintenance-status", headers={"Authorization": "Bearer test-only"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["maintenance_progress"]["available"]
    assert payload["cleanup_policy"] == dict(auto_remove_invalid_accounts=False, renewal_failure_auto_delete=False)
    assert set(payload) == {"maintenance", "maintenance_progress", "cleanup_policy"}
    monkeypatch.setenv("CHATGPT2API_ACCOUNT_SHARD_INDEX", "3")
    monkeypatch.setenv("CHATGPT2API_ACCOUNT_SHARD_COUNT", "8")
    response = client.get("/api/accounts/maintenance-status", headers={"Authorization": "Bearer test-only"})
    assert response.json()["maintenance_progress"]["state"] == "not_owner"
    client.close()
