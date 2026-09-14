from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from threading import Lock

from services.account_service import AccountService


class _SelectionProbe:
    """Minimal AccountService-shaped object for the hot-path selector."""

    _find_available_image_token_locked = AccountService._find_available_image_token_locked
    _image_account_belongs_to_instance = AccountService._image_account_belongs_to_instance
    _is_image_account_available = AccountService._is_image_account_available
    _is_unlimited_image_quota_account = AccountService._is_unlimited_image_quota_account
    _account_matches_plan_type = AccountService._account_matches_plan_type
    _account_matches_any_plan_type = AccountService._account_matches_any_plan_type
    _account_matches_source_type = AccountService._account_matches_source_type

    def __init__(self, accounts: list[dict]) -> None:
        self._accounts = OrderedDict(
            (item["access_token"], item) for item in accounts
        )
        self._image_index = 0
        self._image_shard_count = 1
        self._image_shard_index = 0
        self._image_inflight: dict[str, int] = {}


def _account(token: str, *, quota: int = 0, unknown: bool = True, warm: bool = False) -> dict:
    item = {
        "access_token": token,
        "status": "正常",
        "quota": quota,
        "image_quota_unknown": unknown,
    }
    if warm:
        item["last_image_success_at"] = "2026-09-15T00:00:00+00:00"
    return item


def _select(probe: _SelectionProbe) -> str | None:
    token, _ready, _matched, _limited = probe._find_available_image_token_locked(
        set(), None, None, None
    )
    return token


def test_selection_prefers_confirmed_positive_quota_over_warm_and_cold_unknown(monkeypatch):
    probe = _SelectionProbe([
        _account("cold"),
        _account("warm", warm=True),
        _account("known", quota=8, unknown=False),
    ])

    assert _select(probe) == "known"


def test_selection_prefers_recently_successful_unknown_over_cold_unknown(monkeypatch):
    probe = _SelectionProbe([
        _account("cold"),
        _account("warm", warm=True),
    ])

    assert _select(probe) == "warm"


def test_unknown_quota_scan_is_bounded_oldest_first_and_skips_recent_attempts(monkeypatch):
    probe = AccountService.__new__(AccountService)
    probe._lock = Lock()
    now = datetime.now(timezone.utc)
    probe._accounts = OrderedDict([
        ("new", _account("new")),
        ("old", _account("old")),
        ("recent", _account("recent")),
        ("pending", _account("pending")),
    ])
    probe._accounts["old"]["last_remote_check_attempt_at"] = (
        now - timedelta(hours=2)
    ).isoformat()
    probe._accounts["recent"]["last_remote_check_attempt_at"] = now.isoformat()
    probe._accounts["pending"]["last_remote_check_result"] = "pending"
    monkeypatch.setattr(probe, "_refresh_accounts_snapshot_if_stale", lambda: None)

    assert probe.list_unknown_quota_tokens(limit=2, freshness_seconds=60) == [
        "new",
        "old",
    ]
