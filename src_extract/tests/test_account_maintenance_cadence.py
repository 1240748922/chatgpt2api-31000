"""Deterministic watcher timing; no sleeps, upstream or production account DB."""
from threading import Event
from types import SimpleNamespace

import pytest

from api import support
from services import account_maintenance as maintenance
from services.account_maintenance_progress import AccountMaintenanceProgress
from test_account_auth_quarantine import service_factory, row, now
from test_account_auth_quarantine import jwt


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class Turns(Event):
    def __init__(self, clock, count=2, on_wait=None):
        super().__init__()
        self.clock, self.count, self.on_wait = clock, count, on_wait
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.clock.now += timeout
        if self.on_wait:
            self.on_wait(len(self.waits))
        if len(self.waits) >= self.count:
            self.set()
        return self.is_set()


@pytest.fixture
def watcher(monkeypatch):
    clock = Clock()
    calls = []
    scans = dict(expiry=0, limited=0, quota=0, verify=0, revalidate=0)
    pending = dict(expiry=[], limited=[], quota=[])
    policy = dict(allowed=True, mode="normal", batch_size=2, policy="performance", reason_codes=[])
    cost = {"renew": 3, "sync": 3}
    def selection(kind):
        scans[kind] += 1
        return list(pending[kind])
    def quota(*, candidate_tokens=None, limit=None):
        if candidate_tokens is None:
            return selection("quota")
        scans["revalidate"] += 1
        return [token for token in candidate_tokens if token in pending["quota"]][:limit]
    def verify(**_):
        scans["verify"] += 1
        return 0
    def operation(kind, tokens):
        calls.append((kind, clock.now, list(tokens)))
        clock.now += cost[kind]
        return {"refreshed" if kind == "renew" else "synced": len(tokens)}
    service = SimpleNamespace(
        list_limited_tokens=lambda: selection("limited"),
        list_expiring_access_tokens=lambda: selection("expiry"),
        list_unknown_quota_tokens=quota,
        resume_pending_auth_verifications=verify,
        renew_expiring_access_tokens=lambda tokens: operation("renew", tokens),
        sync_accounts_and_quota=lambda tokens: operation("sync", tokens),
    )
    tracker = AccountMaintenanceProgress()
    monkeypatch.setattr(support, "account_service", service)
    monkeypatch.setattr(support, "config", SimpleNamespace(refresh_account_interval_minute=30))
    monkeypatch.setattr(support, "time", clock)
    monkeypatch.setattr(maintenance, "time", clock)
    monkeypatch.setattr(support, "account_maintenance_progress", tracker)
    monkeypatch.setattr(maintenance, "account_maintenance_progress", tracker)
    monkeypatch.setattr(support, "account_maintenance_decision", lambda: dict(policy))
    monkeypatch.setattr(maintenance, "account_maintenance_decision", lambda: dict(policy))
    monkeypatch.setenv("CHATGPT2API_MAINTENANCE_RETRY_SECONDS", "30")
    monkeypatch.setenv("CHATGPT2API_MAINTENANCE_CONTINUE_SECONDS", "2")
    def run(turns=2, on_wait=None):
        stop = Turns(clock, turns, on_wait)
        thread = support.start_account_lifecycle_watcher(stop)
        thread.join(3)
        assert not thread.is_alive(), "synthetic watcher did not stop"
        assert tracker.snapshot()["state"] == "stopped"
        return stop.waits
    return SimpleNamespace(clock=clock, calls=calls, scans=scans, pending=pending,
                           policy=policy, cost=cost, service=service, tracker=tracker, run=run)


def test_recovery_backlog_continues_without_fixed_thirty_second_gap(watcher):
    watcher.pending["expiry"] = [f"account-{i}" for i in range(70)]
    waits = watcher.run()
    assert waits == [2, 2]
    assert watcher.calls[6][1] == 18  # seventh batch finishes the 20s time slice at 21s
    assert watcher.calls[7][1] == 23  # next slice starts after 2s, not at 51s
    tokens = [token for _, _, batch in watcher.calls for token in batch]
    assert tokens == watcher.pending["expiry"][:28]
    assert watcher.scans == dict(expiry=1, limited=1, quota=1, verify=1, revalidate=0)


def test_quota_backlog_reuses_window_instead_of_rescanning_whole_pool(watcher):
    watcher.pending["quota"] = [f"quota-{i}" for i in range(50)]
    assert watcher.run() == [2, 2]
    assert watcher.scans["quota"] == 1 and watcher.scans["verify"] == 1
    assert watcher.scans["revalidate"] == 1
    tokens = [token for _, _, batch in watcher.calls for token in batch]
    assert tokens == watcher.pending["quota"][:28]


def test_queued_quota_that_was_changed_elsewhere_is_not_dispatched(watcher):
    watcher.pending["quota"] = [f"quota-{i}" for i in range(50)]
    def changed(_):
        # Simulate an already-checked/deleted account in the retained window.
        watcher.pending["quota"] = [t for t in watcher.pending["quota"] if t != "quota-14"]
    watcher.run(on_wait=changed)
    assert watcher.calls[7][2] == ["quota-15", "quota-16"]


@pytest.mark.parametrize("mode", ["reduced", "probe", "paused"])
def test_pressure_or_unknown_metrics_keep_original_retry(watcher, mode):
    watcher.pending["expiry"] = [f"account-{i}" for i in range(50)]
    watcher.policy.update(mode=mode, allowed=mode != "paused", batch_size=0 if mode == "paused" else 1)
    assert watcher.run() == [30, 30]
    assert len(watcher.calls) == (0 if mode == "paused" else 2)


def test_pressure_rising_after_a_slice_prevents_fast_continuation(watcher):
    watcher.pending["expiry"] = [f"account-{i}" for i in range(50)]
    original = watcher.service.renew_expiring_access_tokens
    def renewal(tokens):
        result = original(tokens)
        watcher.policy.update(mode="paused", allowed=False, batch_size=0)
        return result
    watcher.service.renew_expiring_access_tokens = renewal
    assert watcher.run() == [30, 30]
    assert len(watcher.calls) == 1


def test_legacy_policy_does_not_accelerate(watcher):
    watcher.pending["expiry"] = [f"account-{i}" for i in range(50)]
    watcher.policy["policy"] = "idle"
    assert watcher.run() == [30, 30]


def test_empty_pool_keeps_scan_cadence_and_does_not_spin(watcher):
    assert watcher.run() == [30, 30]
    assert not watcher.calls
    assert watcher.scans["quota"] == 2 and watcher.scans["verify"] == 2
    assert watcher.scans["expiry"] == 1


def test_no_progress_does_not_spin_on_same_queue(watcher, monkeypatch):
    watcher.pending["expiry"] = ["synthetic-busy"]
    monkeypatch.setattr(support, "renew_idle_batch", lambda *args: 0)
    assert watcher.run() == [30, 30]
    assert not watcher.calls


def test_exception_after_partial_work_backs_off(watcher):
    watcher.pending["expiry"] = [f"account-{i}" for i in range(50)]
    def broken(**_):
        raise RuntimeError("synthetic quota snapshot unavailable")
    watcher.service.list_unknown_quota_tokens = broken
    assert watcher.run() == [30, 30]


def test_drained_window_uses_discovery_deadline_not_another_full_wait(watcher):
    watcher.pending["quota"] = ["quota-a", "quota-b"]
    watcher.cost["sync"] = 10
    assert watcher.run(turns=1) == [20]  # 10s work + 20s wait, not 10s + 30s


def test_real_wait_is_interruptible_during_shutdown(watcher):
    entered_wait = Event()
    class ObservedStop(Event):
        def wait(self, timeout=None):
            entered_wait.set()
            return super().wait(timeout)
    stop = ObservedStop()
    thread = support.start_account_lifecycle_watcher(stop)
    try:
        assert entered_wait.wait(2)
        assert watcher.tracker.snapshot()["state"] == "waiting"
    finally:
        stop.set()
        thread.join(2)
    assert not thread.is_alive()
    assert watcher.tracker.snapshot()["state"] == "stopped"


@pytest.mark.parametrize("configured,expected", [("1", 1), ("5", 5), ("30", 30), ("999", 30), ("-10", 1), ("bad", 2)])
def test_continuation_configuration_is_bounded_and_has_rollback(watcher, monkeypatch, configured, expected):
    monkeypatch.setenv("CHATGPT2API_MAINTENANCE_CONTINUE_SECONDS", configured)
    watcher.pending["expiry"] = [f"account-{i}" for i in range(50)]
    assert watcher.run(turns=1) == [expected]


def test_revalidation_is_bounded_and_preserves_freshness_busy_and_rotation_guards(service_factory, monkeypatch):
    tokens = ["eligible", "new-at", "known", "recent", "busy", "pending", "disabled", "unrelated"]
    service = service_factory([row(token, image_quota_unknown=True, last_remote_checked_at=None) for token in tokens])
    class NoFullPoolScan(dict):
        def values(self):
            pytest.fail("continuation iterated full account pool")
        def __iter__(self):
            pytest.fail("continuation iterated full account pool")
    monkeypatch.setattr(service, "_accounts", NoFullPoolScan(service._accounts))
    service._accounts["known"]["image_quota_unknown"] = False
    service._accounts["recent"]["last_remote_check_attempt_at"] = now()
    service._accounts["pending"]["last_remote_check_result"] = "pending"
    service._accounts["disabled"]["status"] = "禁用"
    service._image_inflight["busy"] = 1
    service._token_aliases["old-at"] = "new-at"
    refreshes = []
    monkeypatch.setattr(service, "_refresh_accounts_snapshot_if_stale", lambda **kw: refreshes.append(kw))
    result = service.list_unknown_quota_tokens(limit=50, candidate_tokens=[
        "eligible", "old-at", "new-at", "known", "recent", "busy", "pending", "disabled", "deleted",
    ])
    assert result == ["eligible", "new-at"]
    assert refreshes == [{"wait_for_refresh": False, "allow_full_reload": False}]


def test_strict_background_quota_only_uses_ready_at_renewal_keeps_recovery(service_factory, monkeypatch):
    monkeypatch.setenv("CHATGPT2API_STRICT_IMAGE_CREDENTIALS", "1")
    ready, expired, terminal, expiring = jwt(7*86400), jwt(-300), jwt(-301), jwt(10)
    service = service_factory([
        row(ready, image_quota_unknown=True, last_remote_checked_at=None), row(expired, refresh_token="rt", image_quota_unknown=True),
        row(terminal, refresh_token="bad-rt", refresh_token_invalid_at=now(), image_quota_unknown=True),
        row(expiring, refresh_token="expiring-rt", image_quota_unknown=True),
        row("opaque", refresh_token="opaque-rt", image_quota_unknown=True),
    ])
    assert service.list_unknown_quota_tokens() == [ready]
    assert service.list_unknown_quota_tokens(candidate_tokens=[expired, terminal, expiring, "opaque", ready]) == [ready]
    assert expired in service.list_expiring_access_tokens()
    assert "opaque" in service.list_expiring_access_tokens()
    assert terminal not in service.list_expiring_access_tokens()
