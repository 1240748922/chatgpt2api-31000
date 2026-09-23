from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from threading import Lock
import time

import pytest

from services.account_service import AccountService, ImageAccountSelectionError, TerminalRefreshTokenError


class _SelectionProbe:
    """Minimal AccountService-shaped object for the hot-path selector."""

    _find_available_image_token_locked = AccountService._find_available_image_token_locked
    _image_account_belongs_to_instance = AccountService._image_account_belongs_to_instance
    _is_image_account_available = AccountService._is_image_account_available
    _is_unlimited_image_quota_account = AccountService._is_unlimited_image_quota_account
    _account_matches_plan_type = AccountService._account_matches_plan_type
    _account_matches_any_plan_type = AccountService._account_matches_any_plan_type
    _account_matches_source_type = AccountService._account_matches_source_type
    _token_needs_refresh = AccountService._token_needs_refresh

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


def test_funded_ready_token_is_preferred_over_rt_maintenance(monkeypatch):
    stale = {**_account("stale", quota=8, unknown=False), "refresh_token": "synthetic-rt"}
    healthy = _account("healthy", quota=8, unknown=False)
    probe = _SelectionProbe([stale, healthy])
    monkeypatch.setattr(probe, "_token_needs_refresh", lambda token: token == "stale")
    assert _select(probe) == "healthy"
    # Exhausted/busy alternatives still fall back to the refreshable account.
    probe._image_inflight["healthy"] = 1000
    assert _select(probe) == "stale"


def test_rt_preference_preserves_quota_priority_and_bounds_lookahead(monkeypatch):
    rows = [{**_account(f"stale-{i}", quota=8, unknown=False), "refresh_token": "synthetic-rt"}
            for i in range(1000)]
    probe = _SelectionProbe(rows + [_account("healthy", quota=8, unknown=False)])
    checks = []
    monkeypatch.setattr(probe, "_token_needs_refresh", lambda token: checks.append(token) or token != "healthy")
    assert _select(probe) == "stale-0"
    assert len(checks) < 100  # A large expired pool must not be fully scanned.
    probe = _SelectionProbe([_account("unknown", warm=True), rows[0]])
    monkeypatch.setattr(probe, "_token_needs_refresh", lambda token: True)
    assert _select(probe) == "stale-0"


def test_unknown_quota_scan_is_bounded_oldest_first_and_skips_recent_attempts(monkeypatch):
    probe = AccountService.__new__(AccountService)
    probe._lock = Lock()
    probe._image_inflight = {}
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


def test_upload_cooldown_keeps_text_generation_available():
    account = _account("limited-upload", quota=8, unknown=False)
    account["file_upload_blocked_until"] = time.time() + 3600
    probe = _SelectionProbe([account])
    token, ready, _, _ = probe._find_available_image_token_locked(
        set(), None, None, None, requires_file_upload=True,
    )
    assert token is None
    assert ready == 0
    assert _select(probe) == "limited-upload"


def test_image_selection_skips_account_that_fails_token_maintenance():
    class MaintenanceProbe:
        get_available_access_token = AccountService.get_available_access_token
        _IMAGE_POOL_WAIT_SECONDS = 0

        def __init__(self):
            self.selected = iter(("stale", "healthy"))
            self.released = []

        def _refresh_accounts_snapshot_if_stale(self, **_kwargs):
            return False

        def _acquire_next_candidate_token(self, **_kwargs):
            return next(self.selected)

        def ensure_access_token(self, access_token, **_kwargs):
            if access_token == "stale":
                raise TerminalRefreshTokenError(400, "invalid_refresh_token")
            return access_token

        def release_image_slot(self, access_token):
            self.released.append(access_token)

        def _validate_image_lease(self, token, **kwargs):
            return token

    probe = MaintenanceProbe()
    assert probe.get_available_access_token() == "healthy"
    assert probe.released == ["stale"]


@pytest.mark.parametrize("deadline_seconds", [5, 20])
def test_account_lookup_phase_timings_include_internal_retries_and_errors(monkeypatch, deadline_seconds):
    import services.account_service as module
    from test_image_recovery import Clock

    clock = Clock()
    monkeypatch.setattr(module, "time", clock)

    class Probe:
        get_available_access_token = AccountService.get_available_access_token
        _set_image_selection_diagnostics = AccountService._set_image_selection_diagnostics
        get_image_selection_diagnostics = AccountService.get_image_selection_diagnostics

        def __init__(self):
            self.candidates = iter(("stale", "healthy"))
            self.released = []

        def _refresh_accounts_snapshot_if_stale(self, **kwargs):
            assert kwargs["allow_full_reload"] is False
            clock.sleep(2)

        def _acquire_next_candidate_token(self, **kwargs):
            self._set_image_selection_diagnostics(selection_wait_ms=1000)
            clock.sleep(1)
            return next(self.candidates)

        def ensure_access_token(self, token, **kwargs):
            clock.sleep(3 if token == "stale" else 4)
            if token == "stale":
                raise TerminalRefreshTokenError(400, "invalid_refresh_token")
            return token

        def release_image_slot(self, token):
            self.released.append(token)

        def _validate_image_lease(self, token, **kwargs):
            return token

    probe = Probe()
    deadline = clock.now + deadline_seconds
    if deadline_seconds == 5:
        with pytest.raises(ImageAccountSelectionError):
            probe.get_available_access_token(deadline_monotonic=deadline)
    else:
        assert probe.get_available_access_token(deadline_monotonic=deadline) == "healthy"
    diagnostic = probe.get_image_selection_diagnostics()
    assert diagnostic["account_snapshot_check_ms"] == 2000
    assert diagnostic["account_candidate_total_ms"] == (1000 if deadline_seconds == 5 else 2000)
    assert diagnostic["account_token_maintenance_ms"] == (3000 if deadline_seconds == 5 else 7000)
    assert diagnostic["account_candidate_attempts"] == (1 if deadline_seconds == 5 else 2)
    assert diagnostic["selection_wait_ms"] == 1000  # last selection retains its existing meaning
    assert probe.released == ["stale"]
    # The raw event whitelist must retain the phases for saved per-attempt logs.
    from services.realtime_monitor_service import RealtimeMonitorService
    monitor = RealtimeMonitorService.__new__(RealtimeMonitorService)
    event = monitor._event("synthetic-call", "image_account_lookup", {}, diagnostic)
    for key in ("account_snapshot_check_ms", "account_candidate_total_ms",
                "account_token_maintenance_ms", "account_candidate_attempts"):
        assert event[key] == diagnostic[key]


def test_busy_image_pool_exits_short_wait_with_diagnostics(monkeypatch):
    probe = AccountService.__new__(AccountService)
    probe._lock = Lock()
    probe._image_slot_condition = __import__("threading").Condition()
    probe._accounts = OrderedDict([
        ("busy", _account("busy", quota=8, unknown=False)),
    ])
    probe._image_inflight = {"busy": 1}
    probe._image_index = 0
    probe._image_shard_count = 1
    probe._image_shard_index = 0
    probe._IMAGE_POOL_WAIT_SECONDS = 0.01
    probe._account_snapshot_checked_at = time.monotonic()

    started = time.perf_counter()
    with pytest.raises(ImageAccountSelectionError) as caught:
        probe._acquire_next_candidate_token()

    assert caught.value.code == "no_available_account"
    assert time.perf_counter() - started < 0.5
    diagnostics = probe.get_image_selection_diagnostics()
    assert diagnostics["account_wait_reason"] == "all_ready_accounts_busy"
    assert diagnostics["matched_count"] == 1
    assert diagnostics["ready_count"] == 1
    assert diagnostics["busy_count"] == 1
