from __future__ import annotations

from collections import OrderedDict
from threading import RLock

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
