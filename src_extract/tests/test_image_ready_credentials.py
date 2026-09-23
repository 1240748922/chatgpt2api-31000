"""Ready credentials avoid foreground renewal; expired credentials never do.

All accounts are synthetic and storage is temporary SQLite. No upstream I/O.
"""
import time
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event

import pytest

from test_account_auth_quarantine import jwt, now, row, service_factory


def test_image_uses_sufficiently_valid_at_without_refresh_or_writer_lock(service_factory):
    token = jwt(3 * 3600)
    service = service_factory([row(token, refresh_token="synthetic-rt")])
    def select():
        return service.get_available_access_token(), service.get_image_selection_diagnostics()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with service._write_lock:
            result = executor.submit(select)
            try:
                selected, metrics = result.result(timeout=1)
                assert selected == token
            finally:
                service.release_image_slot(token)
    assert metrics["account_token_http_ms"] == 0
    assert metrics["account_token_save_ms"] == 0


def test_background_still_renews_at_inside_24_hour_window(service_factory, monkeypatch):
    old, new = jwt(3 * 3600), jwt(7 * 86400)
    service = service_factory([row(old, refresh_token="synthetic-rt")])
    calls = []
    def refresh(*a, **kw):
        calls.append(1)
        return {"access_token": new, "refresh_token": "new-rt"}
    monkeypatch.setattr(service, "_request_access_token_refresh", refresh)
    assert old in service.list_expiring_access_tokens()
    assert service.ensure_access_token(old) == new
    assert calls == [1]


@pytest.mark.parametrize("lifetime,deadline_seconds", [(60, 180), (-1, 180), (3600, 7200)])
def test_short_lifetime_or_long_request_must_refresh(service_factory, monkeypatch, lifetime, deadline_seconds):
    old, new = jwt(lifetime), jwt(7 * 86400)
    service = service_factory([row(old, refresh_token="synthetic-rt")])
    monkeypatch.setattr(service, "_request_access_token_refresh", lambda *a, **kw: {
        "access_token": new, "refresh_token": "new-rt"})
    assert service.get_available_access_token(deadline_monotonic=time.monotonic() + deadline_seconds) == new
    service.release_image_slot(old)
    assert service._image_inflight == {}
    assert service.storage.load_accounts()[0]["access_token"] == new


def test_ready_unknown_quota_beats_funded_but_expired_account(service_factory):
    expired, ready = jwt(-3600), jwt(7200)
    service = service_factory([row(expired, refresh_token="synthetic-rt"),
                               row(ready, image_quota_unknown=True, quota=0)])
    assert service.get_available_access_token() == ready
    service.release_image_slot(ready)


def test_no_rt_near_expiry_is_deprioritized_but_not_permanently_disabled(service_factory):
    short, ready = jwt(30), jwt(7200)
    service = service_factory([row(short), row(ready, image_quota_unknown=True, quota=0)])
    assert service.get_available_access_token() == ready
    service.release_image_slot(ready)
    assert service._accounts[short]["status"] == "正常"
    # When it is the only candidate, retaining an unexpired AT is safer than
    # turning a recoverable pool shortage into an immediate failure.
    service._image_inflight[ready] = 10000
    assert service.get_available_access_token() == short
    service.release_image_slot(short)


def test_selection_avoids_an_account_already_being_refreshed(service_factory):
    old, ready = jwt(7 * 86400), jwt(8 * 86400)
    service = service_factory([row(old, refresh_token="synthetic-rt"), row(ready)])
    key = service._credential_generation(old, service._accounts[old])
    service._oauth_refresh_flights[key] = Future()
    assert service.get_available_access_token(deadline_monotonic=time.monotonic() + 1) == ready
    service.release_image_slot(ready)


def test_refresh_fallback_remains_available_when_ready_accounts_busy(service_factory, monkeypatch):
    old, ready, new = jwt(-3600), jwt(7200), jwt(7 * 86400)
    service = service_factory([row(old, refresh_token="synthetic-rt"), row(ready)])
    service._image_inflight[ready] = 10000
    monkeypatch.setattr(service, "_request_access_token_refresh", lambda *a, **kw: {
        "access_token": new, "refresh_token": "new-rt"})
    assert service.get_available_access_token() == new
    service.release_image_slot(new)


def test_fresh_token_is_not_dispatched_until_database_commit(service_factory, monkeypatch):
    old, new = jwt(-3600), jwt(7 * 86400)
    service = service_factory([row(old, refresh_token="synthetic-rt")])
    entered, release = Event(), Event()
    original = service.storage.mutate_accounts_checked
    def commit(*a, **kw):
        entered.set()
        assert release.wait(5)
        return original(*a, **kw)
    monkeypatch.setattr(service.storage, "mutate_accounts_checked", commit)
    monkeypatch.setattr(service, "_request_access_token_refresh", lambda *a, **kw: {
        "access_token": new, "refresh_token": "new-rt"})
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(service.get_available_access_token)
        try:
            assert entered.wait(2)
            assert not result.done()
            assert new not in service._accounts
        finally:
            release.set()
        assert result.result(timeout=2) == new
    service.release_image_slot(new)


def test_background_expiry_scan_prioritizes_urgent_and_skips_backoff(service_factory):
    later, urgent, backed_off = jwt(3600), jwt(-3600), jwt(-7200)
    service = service_factory([
        row(later, refresh_token="test-rt"),
        row(backed_off, refresh_token="test-rt", last_token_refresh_error_at=now()),
        row(urgent, refresh_token="test-rt"),
    ])
    assert service.list_expiring_access_tokens() == [urgent, later]


def test_background_renewal_does_not_rotate_an_active_image_lease(service_factory):
    token = jwt(3600)
    service = service_factory([row(token, refresh_token="synthetic-rt")])
    service._image_inflight[token] = 1
    result = service.renew_expiring_access_tokens([token])
    assert result["refreshed"] == 0
    assert result["skipped"] == 1
    assert result["errors"] == []
    assert service._image_inflight[token] == 1


def test_same_account_refresh_remains_singleflight_when_no_alternative(service_factory, monkeypatch):
    old, new = jwt(-3600), jwt(7 * 86400)
    service = service_factory([row(old, refresh_token="synthetic-rt")])
    import services.account_service as account_module
    from types import SimpleNamespace
    monkeypatch.setattr(account_module, "config", SimpleNamespace(
        image_account_concurrency=2, image_request_timeout_secs=240))
    entered, release = Event(), Event()
    calls = []
    def refresh(*a, **kw):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return {"access_token": new, "refresh_token": "new-rt"}
    monkeypatch.setattr(service, "_request_access_token_refresh", refresh)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.get_available_access_token)
        try:
            assert entered.wait(2)
            second = executor.submit(service.get_available_access_token)
        finally:
            release.set()
        assert first.result(timeout=2) == second.result(timeout=2) == new
    assert calls == [1]
    service.release_image_slot(old)
    service.release_image_slot(new)
    assert service._image_inflight == {}


def test_concurrent_ready_requests_do_not_queue_behind_account_writer(service_factory):
    service = service_factory([row(jwt(10800 + i), refresh_token="synthetic-rt") for i in range(80)])
    def dispatch(_):
        token = service.get_available_access_token()
        metrics = service.get_image_selection_diagnostics()
        service.release_image_slot(token)
        assert metrics["account_token_http_ms"] == 0
        assert metrics["account_token_save_ms"] == 0
        return token
    with ThreadPoolExecutor(max_workers=16) as executor:
        with service._write_lock:
            pending = [executor.submit(dispatch, i) for i in range(128)]
            for request in pending:
                assert request.result(timeout=3)
    assert service._image_inflight == {}
