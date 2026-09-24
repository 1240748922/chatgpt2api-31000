import base64
import json
import time
from copy import deepcopy

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
    assert result.json()["generation_quota"] == dict(known_remaining=0, known_accounts=0, unknown_accounts=0, unlimited_accounts=0)
    assert result.json()["edit_quota"] == result.json()["generation_quota"]
    assert "synthetic-secret" not in result.text
    assert result.json()["maintenance"]["policy"] == "performance"


def test_ready_quota_excludes_unready_limits_and_separates_unknown_unlimited(service_factory, monkeypatch):
    now = int(time.time())
    service = service_factory([
        row(token(now+7200), quota=8),
        row(token(now+7201), quota=12, file_upload_blocked_until=now+600),
        row("opaque", quota=999), row(token(now-1), quota=999), row(token(now+10), quota=999),
        row(token(now+7202), quota=999, status="异常"),
        row(token(now+7203), quota=999, status="禁用"),
        row(token(now+7204), quota=999, last_remote_check_result="pending", pending_auth_scope="image"),
        row(token(now+7205), quota=999, last_remote_check_result="invalid"),
        row(token(now+7206), quota=999, status="限流"),
        row(token(now+7207), quota=999, image_quota_unknown=True),
        row(token(now+7208), quota=999, image_quota_unknown=True, type="pro"),
        row(token(now+7209), quota=0),
        row(token(now+7210), quota=999, image_quota_unknown=True, type="prolite", file_upload_blocked_until=now+600),
    ])
    monkeypatch.setattr(service, "_refresh_accounts_snapshot_if_stale", lambda **kw: False)
    before = deepcopy(service._accounts)
    result = service.readiness_summary()
    assert result["generation_candidates"] == 6
    assert result["edit_candidates"] == 4
    assert result["generation_quota"] == dict(known_remaining=20, known_accounts=2, unknown_accounts=2, unlimited_accounts=2)
    assert result["edit_quota"] == dict(known_remaining=8, known_accounts=1, unknown_accounts=2, unlimited_accounts=1)
    assert service._accounts == before  # Observation must never consume quota or change eligibility.
    result["generation_quota"]["known_remaining"] = 999
    assert service.readiness_summary()["generation_quota"]["known_remaining"] == 20


def test_ready_quota_empty_pool_is_known_zero(service_factory):
    result = service_factory([]).readiness_summary()
    assert result["generation_quota"] == result["edit_quota"] == dict(
        known_remaining=0, known_accounts=0, unknown_accounts=0, unlimited_accounts=0)


def test_ready_quota_expired_upload_cooldown_does_not_remove_edit_quota(service_factory):
    now = int(time.time())
    result = service_factory([row(token(now+7200), quota=7, file_upload_blocked_until=now-1)]).readiness_summary()
    assert result["generation_quota"] == result["edit_quota"] == dict(
        known_remaining=7, known_accounts=1, unknown_accounts=0, unlimited_accounts=0)


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
