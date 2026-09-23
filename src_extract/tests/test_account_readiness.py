import base64
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.account_readiness import credential_readiness, summarize_readiness
from test_account_auth_quarantine import service_factory, row


def token(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return "e30." + payload + ".synthetic"


@pytest.mark.parametrize("expiry,state", [(2000, "ready"), (1301, "ready"), (1300, "expiring"), (1001, "expiring"), (1000, "expired"), (999, "expired"), (None, "unknown"), (10**100, "unknown"), (float("inf"), "unknown"), (True, "unknown")])
def test_readiness_expiry_boundary(expiry, state):
    value = credential_readiness(row(token(expiry)), 300, now=1000)
    assert value["state"] == state


def test_quota_unknown_is_not_expiry_unknown():
    item = row(token(10000), image_quota_unknown=True, quota=0)
    assert credential_readiness(item, 300, now=1000)["state"] == "ready"
    assert credential_readiness(row("opaque"), 300, now=1000)["state"] == "unknown"


def test_states_are_exclusive_and_credentials_are_never_returned():
    items = [row(token(2000)), row("opaque", refresh_token="private-rt"), row(token(1100)), row(token(900)),
             row(token(2100), last_remote_check_result="pending"), row(token(2200), status="异常"),
             row(token(2300), status="禁用")]
    result = summarize_readiness(items, 300, now=1000)
    assert result["counts"] == {state: (0 if state in {"refreshing", "uncertain"} else 1) for state in result["counts"]}
    assert sum(result["counts"].values()) == result["total"] == 7
    assert result["refresh_candidates"] == 1
    assert result["needs_credentials"] == 4
    assert "private-rt" not in json.dumps(result)
    assert "access_token" not in json.dumps(result)


def test_summary_excludes_quota_and_upload_limits_without_changing_dispatch(service_factory, monkeypatch):
    now = int(time.time())
    service = service_factory([
        row(token(now+7200)), row(token(now+7201), file_upload_blocked_until=now+600),
        row(token(now+7202), status="限流", quota=0), row("opaque"),
    ])
    monkeypatch.setattr(service, "_refresh_accounts_snapshot_if_stale", lambda **kw: False)
    result = service.readiness_summary()
    assert result["counts"]["ready"] == 3
    assert result["generation_candidates"] == 2
    assert result["edit_candidates"] == 1
    assert result["policy"] == "readiness_preview"
    assert result["total"] == 4
    assert service._is_image_account_available(service.get_account("opaque"))  # Observation rollout only.
    result["counts"]["ready"] = 999
    assert service.readiness_summary()["counts"]["ready"] == 3


def test_availability_api_requires_admin_and_does_not_expose_credentials(service_factory, monkeypatch):
    from api import accounts
    service = service_factory([row("opaque", refresh_token="synthetic-secret")])
    monkeypatch.setattr(accounts, "account_service", service)
    app = FastAPI()
    app.include_router(accounts.create_router())
    with TestClient(app) as client:
        assert client.get("/api/accounts/availability").status_code == 401
        result = client.get("/api/accounts/availability", headers={"Authorization":"Bearer test-only"})
    assert result.status_code == 200
    assert result.json()["counts"]["unknown"] == 1
    assert "synthetic-secret" not in result.text
    assert result.json()["maintenance"]["policy"] == "performance"


def test_inventory_classification_does_not_hold_foreground_dispatch_lock(service_factory, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from services import account_readiness
    started, release = Event(), Event()
    ready = token(int(time.time()) + 7200)
    service = service_factory([row(ready)])
    monkeypatch.setattr(service, "_refresh_accounts_snapshot_if_stale", lambda **kw: False)
    original = account_readiness.credential_readiness
    def classify(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(account_readiness, "credential_readiness", classify)
    with ThreadPoolExecutor(max_workers=2) as executor:
        summary = executor.submit(service.readiness_summary)
        try:
            assert started.wait(2)
            selected = executor.submit(service.get_available_access_token).result(timeout=1)
            assert selected == ready
            service.release_image_slot(selected)
        finally:
            release.set()
        assert summary.result(timeout=2)["counts"]["ready"] == 1


def test_unknown_and_pending_at_are_not_presented_as_proven_valid():
    from services.account_view import account_row
    item = account_row(row("opaque"), available=True, unlimited_quota=False)
    assert item["access_token_label"] == "AT 有效期未知"
    assert item["image_readiness"]["state"] == "unknown"
    item = account_row(row(token(int(time.time())+864000), last_remote_check_result="pending"),
                       available=False, unlimited_quota=False)
    assert item["access_token_label"] == "AT 待核验"
    assert item["image_readiness"]["state"] == "quarantined"
