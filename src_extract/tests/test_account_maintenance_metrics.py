from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from curl_cffi import requests

from services import account_maintenance_metrics as metrics
from services import account_service as accounts
from services.account_service import AccountService
from services.proxy_service import proxy_settings
from test_account_write_contention import account_flow
from test_database_snapshot_concurrency import databases
from test_image_recovery import Clock


def test_actual_refresh_separates_slot_http_and_persistence(account_flow, monkeypatch):
    service = account_flow.service
    clock = Clock()
    monkeypatch.setattr(accounts, "time", clock)
    monkeypatch.setattr(metrics, "time", clock)
    closed = []
    @contextmanager
    def slot():
        clock.sleep(2)
        yield
    def post(*a, **kw):
        assert kw["timeout"] <= 18
        clock.sleep(3)
        return SimpleNamespace(status_code=200, text="{}", json=lambda: {
            "access_token": "new-test-token", "refresh_token": "new-test-refresh"})
    monkeypatch.setattr(requests, "Session", lambda **kw: SimpleNamespace(post=post, close=lambda: closed.append(True)))
    monkeypatch.setattr(proxy_settings, "build_session_kwargs", lambda **kw: {})
    monkeypatch.setattr(service, "_request_access_token_refresh", lambda *a, **kw:
                        AccountService._request_access_token_refresh(service, *a, request_slot=slot, **kw))
    original_save = service._apply_refreshed_tokens
    @metrics.token_phase("account_token_save_ms")
    def save(*a, **kw):
        clock.sleep(1)
        return original_save(*a, **kw)
    monkeypatch.setattr(service, "_apply_refreshed_tokens", save)
    token = service.get_available_access_token(deadline_monotonic=1020)
    assert token == "new-test-token"
    timings = service.get_image_selection_diagnostics()
    assert timings["account_token_maintenance_ms"] == 6000
    assert timings["account_token_slot_ms"] == 2000
    assert timings["account_token_http_ms"] == 3000
    assert timings["account_token_save_ms"] == 1000
    assert timings["account_token_singleflight_ms"] == 0
    assert closed == [True]
    assert metrics._timings.get() is None
    service.release_image_slot(token)


def test_waiting_for_existing_refresh_is_not_counted_as_http(account_flow, monkeypatch):
    service = account_flow.service
    clock = Clock()
    monkeypatch.setattr(accounts, "time", clock)
    monkeypatch.setattr(metrics, "time", clock)
    key = service._credential_generation("old-test-token", service._accounts["old-test-token"])
    def result(**kwargs):
        clock.sleep(4)
        return "old-test-token"
    service._oauth_refresh_flights[key] = SimpleNamespace(result=result)
    monkeypatch.setattr(service, "_refresh_access_token_owner", lambda *a, **kw: pytest.fail("duplicate refresh"))
    token = service.get_available_access_token(deadline_monotonic=1010)
    timings = service.get_image_selection_diagnostics()
    assert timings["account_token_singleflight_ms"] == timings["account_token_maintenance_ms"] == 4000
    assert timings["account_token_http_ms"] == 0
    assert metrics._timings.get() is None
    service.release_image_slot(token)


def test_collection_publishes_failure_metrics_and_does_not_leak_to_next_call(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(metrics, "time", clock)
    class Probe:
        def __init__(self):
            self.result = {}
        def _set_image_selection_diagnostics(self, **kwargs):
            self.result.update(kwargs)
        @metrics.collect_token_timings
        def run(self, fail):
            if fail:
                with metrics.token_phase("account_token_http_ms"):
                    clock.sleep(3)
                    raise TimeoutError("synthetic refresh timeout")
    probe = Probe()
    with pytest.raises(TimeoutError):
        probe.run(True)
    assert probe.result == {key: 3000 if key == "account_token_http_ms" else 0
                            for key in metrics.TOKEN_METRIC_LABELS}
    probe.run(False)
    assert probe.result == {key: 0 for key in metrics.TOKEN_METRIC_LABELS}
    assert metrics._timings.get() is None


def test_new_token_metrics_survive_monitor_projection_and_contract():
    from services.realtime_monitor_service import RealtimeMonitorService
    from services.monitor_view import _project_event
    from api.monitor_contract import MonitorEventView
    monitor = RealtimeMonitorService()
    monitor.start("token", endpoint="/v1/images/generations", model="fixture")
    values = {key: 123 for key in metrics.TOKEN_METRIC_LABELS}
    monitor.stage("token", "image_account_lookup", **values)
    parsed = MonitorEventView.model_validate(_project_event(monitor._events[-1]))
    assert all(getattr(parsed, key) == value for key, value in values.items())


def test_save_metrics_separate_lock_commit_and_log(account_flow, monkeypatch):
    service = account_flow.service
    clock = Clock()
    monkeypatch.setattr(accounts, "time", clock)
    monkeypatch.setattr(metrics, "time", clock)
    actual_lock = service._write_lock
    class DelayedLock:
        def acquire(self):
            clock.sleep(1)
            return actual_lock.acquire()
        def release(self):
            actual_lock.release()
    monkeypatch.setattr(service, "_write_lock", DelayedLock())
    original = service.storage.mutate_accounts_checked
    def commit(*args, **kwargs):
        clock.sleep(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(service.storage, "mutate_accounts_checked", commit)
    monkeypatch.setattr(accounts.log_service, "add", lambda *a, **kw: clock.sleep(2))
    # Exercise the timing boundaries with an inline diagnostic sink; production
    # foreground success events now enqueue rather than wait for this DB write.
    monkeypatch.setattr(accounts, "defer_credential_log", lambda write, *a: write(*a))
    token = service.get_available_access_token()
    timings = service.get_image_selection_diagnostics()
    assert timings["account_token_write_wait_ms"] == 1000
    assert timings["account_token_writer_lock_ms"] == 1000
    assert timings["account_token_dispatch_lock_ms"] == 0
    assert timings["account_token_commit_ms"] == 3000
    assert timings["account_token_log_ms"] == 2000
    assert timings["account_token_save_ms"] == timings["account_token_maintenance_ms"] == 6000
    service.release_image_slot(token)
