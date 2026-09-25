"""Dashboard and management must agree without changing scheduling or credentials."""
from collections import OrderedDict
from copy import deepcopy
from threading import RLock
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cleanup_fixtures import cleanup_accounts
from dashboard_fixtures import dashboard_payload
from services.account_service import AccountService
from services.account_view import account_row, account_status_category


def service_for(monkeypatch, rows):
    service = AccountService.__new__(AccountService)
    service._lock = RLock()
    service._accounts = OrderedDict((row["access_token"], row) for row in rows)
    service._image_inflight = {}
    service._cumulative_total = len(rows) + 100
    monkeypatch.setattr(service, "_refresh_accounts_snapshot_if_stale", lambda: None)
    return service


def test_dashboard_counts_derived_abnormal_accounts_and_excludes_their_quota(monkeypatch):
    rows = cleanup_accounts()
    before = deepcopy(rows)
    service = service_for(monkeypatch, rows)
    stats = service.get_stats()
    assert stats["abnormal"] == 4  # Old raw-status count reports only one.
    assert stats["active"] == 3
    assert stats["limited"] == 1
    assert stats["disabled"] == 1
    assert sum(stats[key] for key in ("active", "limited", "abnormal", "disabled")) == stats["total"] == 9
    assert stats["total_quota"] == 21  # Recoverable + healthy + ordinary timeout.
    assert stats["cumulative_total"] == 109
    preview = service.preview_auto_remove_accounts(
        remove_invalid=True, remove_unusable_credentials=True, remove_rate_limited=False,
        remove_quota_exhausted=False,
    )
    assert preview["total_removed"] == stats["abnormal"]
    assert rows == before


@pytest.mark.parametrize("changes, expected", [
    ({}, "abnormal"),
    ({"refresh_token": "test-recoverable"}, "normal"),
    ({"refresh_token": "test-recheck", "last_token_refresh_error":
      "oauth_refresh_http_401: refresh_token_reused: historical error"}, "normal"),
    ({"refresh_token": "test-revoked", "refresh_token_invalid_at": "2026-01-01T00:00:00Z"}, "abnormal"),
    ({"access_token": "opaque-unknown-expiry"}, "normal"),
    ({"access_token": "opaque-unknown-expiry", "last_remote_check_result": "invalid"}, "abnormal"),
    ({"access_token": "opaque-unknown-expiry", "last_remote_check_result": "pending"}, "normal"),
    ({"access_token": "opaque-unknown-expiry", "last_remote_check_result": "error",
      "last_refresh_error": "Image generation timed out"}, "normal"),
    ({"status": "限流"}, "abnormal"),
    ({"status": "限流", "refresh_token": "test-recoverable"}, "limited"),
    ({"status": "禁用"}, "disabled"),
    ({"status": "异常", "refresh_token": "test-recoverable"}, "abnormal"),
])
def test_dashboard_and_account_row_use_same_category(monkeypatch, changes, expected):
    row = {**cleanup_accounts()[0], **changes}
    service = service_for(monkeypatch, [row])
    stats = service.get_stats()
    assert account_status_category(row) == expected
    assert account_row(row, available=False, unlimited_quota=False)["status_category"] == expected
    field = "active" if expected == "normal" else expected
    assert stats[field] == 1
    assert sum(stats[key] for key in ("active", "limited", "abnormal", "disabled")) == 1


def test_expiry_and_new_credentials_change_counts_without_persisting_status(monkeypatch):
    import base64
    import json
    from services import account_credentials, account_service

    clock = [1000]
    monkeypatch.setattr(account_credentials, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(account_service, "time", SimpleNamespace(time=lambda: clock[0]))
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1100}).encode()).decode().rstrip("=")
    row = dict(access_token=f"test.{payload}.fake", status="正常", quota=5,
               refresh_token="test-revoked", refresh_token_invalid_at="2026-01-01T00:00:00Z")
    service = service_for(monkeypatch, [row])
    # Invalid RT does not invalidate a still-usable AT, even during upload cooldown.
    row["file_upload_blocked_until"] = 1200
    assert service.get_stats()["active"] == 1
    assert service.get_stats()["upload_limited"] == 1
    clock[0] = 1100
    assert service.get_stats()["abnormal"] == 1
    assert service.get_stats()["total_quota"] == 0
    row.update(refresh_token="test-replacement", refresh_token_invalid_at=None)
    assert service.get_stats()["active"] == 1
    assert row["status"] == "正常"


def test_invalid_credentials_do_not_inflate_unknown_or_unlimited_quota(monkeypatch):
    rows = cleanup_accounts()[:2]
    rows[0].update(image_quota_unknown=True, quota=0)
    rows[1].update(image_quota_unknown=True, quota=0, type="pro")
    rows.append(dict(access_token="test-valid-unknown", status="正常", image_quota_unknown=True, quota=0))
    rows.append(dict(access_token="test-valid-unlimited", status="正常", image_quota_unknown=True, quota=0, type="pro"))
    stats = service_for(monkeypatch, rows).get_stats()
    assert stats["abnormal"] == 2
    assert stats["unknown_quota_count"] == stats["unlimited_quota_count"] == 1


@pytest.mark.parametrize("rows", [[], cleanup_accounts()[:2]])
def test_empty_or_unusable_pool_is_not_reported_healthy(monkeypatch, rows):
    service = service_for(monkeypatch, rows)
    stats = service.account_health()
    assert stats["total"] == stats["abnormal"] == len(rows)
    assert stats["active"] == stats["total_quota"] == 0
    assert stats["healthy"] is False
    assert stats["status"] == "degraded"


def test_counting_does_not_build_diagnostic_details_or_hold_account_lock(monkeypatch):
    from services import account_view

    service = service_for(monkeypatch, cleanup_accounts())
    classify = account_view.account_status_category

    def checked_category(*args, **kwargs):
        assert not service._lock._is_owned()
        return classify(*args, **kwargs)

    monkeypatch.setattr(account_view, "account_status_category", checked_category)
    monkeypatch.setattr(account_view, "_credential_lifecycle", lambda *_: pytest.fail("full row projection"))
    monkeypatch.setattr(account_view, "_diagnostic", lambda *_: pytest.fail("diagnostic sanitization"))
    assert service.get_stats()["abnormal"] == 4


def test_real_dashboard_api_matches_management_filter(monkeypatch):
    from api import accounts, system
    from services import dashboard_view

    service = service_for(monkeypatch, cleanup_accounts())
    sample = dashboard_payload()
    monkeypatch.setattr(dashboard_view, "account_service", service)
    monkeypatch.setattr(dashboard_view, "dashboard_metrics_service", SimpleNamespace(
        snapshot_many=lambda: {key: sample[key] for key in ("metrics", "ranges")}))
    monkeypatch.setattr(dashboard_view, "config", SimpleNamespace(
        get_storage_backend=lambda: SimpleNamespace(get_backend_info=lambda: {}),
        get_image_storage_settings=lambda: {}))
    monkeypatch.setattr(dashboard_view, "runtime_environment_snapshot", lambda: sample["runtime"])
    monkeypatch.setattr(dashboard_view, "cluster_operations_snapshot", lambda: sample["operations"])
    monkeypatch.setattr(system, "require_admin", lambda _: None)
    app = FastAPI()
    app.include_router(system.create_router("test"))
    with TestClient(app) as client:
        response = client.get("/api/dashboard")
    assert response.status_code == 200
    stats = response.json()["accounts"]
    for category, field in [("normal", "active"), ("limited", "limited"),
                            ("abnormal", "abnormal"), ("disabled", "disabled")]:
        page, count, total = service.list_accounts_page(
            page=1, page_size=1,
            predicate=lambda row: accounts._status_matches_filter(row, category),
        )
        assert stats[field] == count
        assert stats["total"] == total
        assert len(page) == 1
    assert stats["abnormal"] == 4
