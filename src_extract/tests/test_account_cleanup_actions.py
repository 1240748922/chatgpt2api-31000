from __future__ import annotations

from collections import OrderedDict
from threading import RLock
import json

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from api import system

from services.account_service import AccountService


def _service(accounts: list[dict]) -> AccountService:
    service = AccountService.__new__(AccountService)
    service._lock = RLock()
    service._accounts = OrderedDict(
        (item["access_token"], item) for item in accounts
    )
    return service


def test_quota_cleanup_only_targets_confirmed_exhausted_accounts(monkeypatch):
    service = _service([
        {"access_token": "invalid", "status": "异常", "quota": 0, "image_quota_unknown": True},
        {"access_token": "limited", "status": "限流", "quota": 0, "image_quota_unknown": False},
        {"access_token": "unknown", "status": "正常", "quota": 0, "image_quota_unknown": True},
        {"access_token": "positive", "status": "正常", "quota": 3, "image_quota_unknown": False},
    ])
    monkeypatch.setattr(service, "_refresh_accounts_snapshot_if_stale", lambda: None)

    preview = service.preview_auto_remove_accounts(
        remove_invalid=False,
        remove_rate_limited=False,
        remove_quota_exhausted=True,
    )
    assert preview["quota_exhausted"] == 1
    assert preview["total_removed"] == 1

    deleted: list[str] = []
    monkeypatch.setattr(service, "delete_accounts", lambda tokens, return_items=False: deleted.extend(tokens) or {"removed": len(tokens)})
    result = service.cleanup_auto_remove_accounts(
        remove_invalid=False,
        remove_rate_limited=False,
        remove_quota_exhausted=True,
    )
    assert deleted == ["limited"]
    assert result["total_removed"] == 1


def test_combined_cleanup_does_not_count_limited_account_twice(monkeypatch):
    service = _service([
        {"access_token": "limited", "status": "限流", "quota": 0, "image_quota_unknown": False},
    ])
    monkeypatch.setattr(service, "_refresh_accounts_snapshot_if_stale", lambda: None)
    preview = service.preview_auto_remove_accounts(
        remove_rate_limited=True,
        remove_quota_exhausted=True,
    )
    assert preview["rate_limited"] == 1
    assert preview["quota_exhausted"] == 0
    assert preview["total_removed"] == 1


def test_cleanup_preview_pages_status_and_quota_without_credentials(monkeypatch):
    accounts = [
        {"access_token": f"secret-at-{i}", "refresh_token": "secret-rt", "cookie": "secret-cookie",
         "email": f"account-{i}@example.test", "status": "异常", "quota": i,
         "image_quota_unknown": i == 0, "management_id": str(i)} for i in range(120)
    ]
    service = _service(accounts)
    monkeypatch.setattr(service, "_refresh_accounts_snapshot_if_stale", lambda: None)
    preview = service.preview_auto_remove_accounts(remove_invalid=True, remove_rate_limited=False,
                                                    limit=50, offset=50)
    assert preview["total_removed"] == 120
    assert len(preview["items"]) == 50
    assert preview["items"][0]["email"] == "account-50@example.test"
    assert preview["items"][0]["status"] == "异常"
    assert preview["items"][0]["quota"] == 50
    assert preview["has_more"]
    assert "secret-" not in json.dumps(preview)
    first = service.preview_auto_remove_accounts(remove_invalid=True, limit=1)
    assert first["items"][0]["quota_label"] == "未知"


def test_combined_invalid_and_quota_cleanup_counts_each_account_once(monkeypatch):
    service = _service([{"access_token": "invalid", "status": "异常", "quota": 0}])
    monkeypatch.setattr(service, "_refresh_accounts_snapshot_if_stale", lambda: None)
    preview = service.preview_auto_remove_accounts(remove_invalid=True, remove_quota_exhausted=True)
    assert preview["total_removed"] == len(preview["items"]) == 1


def test_credentials_cleanup_targets_only_unrecoverable_at_and_rt(monkeypatch):
    service = _service([
        {
            "access_token": "dead-both",
            "refresh_token": "revoked-rt",
            "status": "正常",
            "last_remote_check_result": "invalid",
            "refresh_token_invalid_at": "2026-09-20T00:00:00+00:00",
            "email": "dead-both@example.test",
        },
        {
            "access_token": "dead-missing-rt",
            "refresh_token": "",
            "status": "正常",
            "last_remote_check_result": "invalid",
            "email": "dead-missing-rt@example.test",
        },
        {
            "access_token": "recoverable",
            "refresh_token": "still-valid-rt",
            "status": "正常",
            "last_remote_check_result": "invalid",
        },
        {
            "access_token": "at-still-valid",
            "refresh_token": "revoked-rt",
            "status": "正常",
            "refresh_token_invalid_at": "2026-09-20T00:00:00+00:00",
        },
        {
            "access_token": "disabled-dead",
            "refresh_token": "",
            "status": "禁用",
            "last_remote_check_result": "invalid",
        },
    ])
    monkeypatch.setattr(service, "_refresh_accounts_snapshot_if_stale", lambda: None)

    preview = service.preview_auto_remove_accounts(
        remove_invalid=False,
        remove_rate_limited=False,
        remove_quota_exhausted=False,
        remove_unusable_credentials=True,
    )

    assert preview["credentials_unavailable"] == 2
    assert preview["total_removed"] == 2
    assert {item["email"] for item in preview["items"]} == {
        "dead-both@example.test",
        "dead-missing-rt@example.test",
    }
    assert all(item["cleanup_reason"] == "AT/RT 失效" for item in preview["items"])
    assert {item["id"] for item in preview["items"]} == {
        service._management_id_for_token("dead-both"),
        service._management_id_for_token("dead-missing-rt"),
    }

    deleted: list[str] = []
    monkeypatch.setattr(
        service,
        "delete_accounts",
        lambda tokens, return_items=False: deleted.extend(tokens) or {"removed": len(tokens)},
    )
    result = service.cleanup_auto_remove_accounts(
        remove_invalid=False,
        remove_rate_limited=False,
        remove_quota_exhausted=False,
        remove_unusable_credentials=True,
    )
    assert deleted == ["dead-both", "dead-missing-rt"]
    assert result["credentials_unavailable"] == 2
    assert result["total_removed"] == 2


def test_cleanup_preview_api_validates_pagination_and_never_deletes(monkeypatch):
    service = _service([{"access_token": "token", "status": "限流", "quota": 0}])
    monkeypatch.setattr(service, "_refresh_accounts_snapshot_if_stale", lambda: None)
    monkeypatch.setattr(system, "account_service", service)
    def auth(value):
        if value != "test-only":
            raise HTTPException(status_code=403)
    monkeypatch.setattr(system, "require_admin", auth)
    app = FastAPI()
    app.include_router(system.create_router("test"))
    client = TestClient(app)
    payload = {"auto_remove_invalid_accounts": False, "auto_remove_rate_limited_accounts": False,
               "remove_quota_exhausted": True, "preview_limit": 1, "preview_offset": 0}
    url = "/api/settings/account-cleanup/preview"
    assert client.post(url, json=payload).status_code == 403
    response = client.post(url, json=payload, headers={"Authorization": "test-only"})
    assert response.status_code == 200
    assert response.json()["items"][0]["quota_label"] == "0"
    assert "token" in service._accounts
    assert client.post(url, json={**payload, "preview_limit": 300},
                       headers={"Authorization": "test-only"}).status_code == 422
