from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from services import credential_event_log as events
from services.bounded_task_runner import BoundedTaskRunner
from test_account_auth_quarantine import jwt, row, service_factory


@pytest.fixture
def log_runner(monkeypatch):
    runner = BoundedTaskRunner(name="test-credential-log", max_workers=1, queue_size=1)
    monkeypatch.setattr(events, "credential_log_runner", runner)
    yield runner
    runner.shutdown(wait=True)


def test_slow_diagnostic_does_not_delay_committed_token(service_factory, monkeypatch, log_runner):
    import services.account_service as accounts
    old, new = jwt(-3600), jwt(7 * 86400)
    service = service_factory([row(old, refresh_token="test-rt")])
    monkeypatch.setattr(service, "_request_access_token_refresh", lambda *a, **kw: {
        "access_token": new, "refresh_token": "new-test-rt"})
    entered, release = Event(), Event()
    messages = []
    def log(*args):
        messages.append(args)
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(accounts.log_service, "add", log)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(service.get_available_access_token)
        try:
            assert entered.wait(2)
            assert result.result(timeout=1) == new
            assert service.storage.load_accounts()[0]["access_token"] == new
            assert new not in str(messages)
            assert "new-test-rt" not in str(messages)
        finally:
            release.set()
    service.release_image_slot(new)


def test_log_queue_overload_does_not_block_or_grow(log_runner, caplog):
    entered, release = Event(), Event()
    written = []
    def write(*args):
        entered.set()
        assert release.wait(5)
        written.append(args)
    try:
        events.defer_credential_log(write, "account", "test", {"source": "fixture"})
        assert entered.wait(2)
        events.defer_credential_log(write, "account", "test", {"source": "fixture"})
        for _ in range(5):
            events.defer_credential_log(write, "account", "test", {"source": "fixture"})
        assert log_runner.status()["accepted"] == 2
        assert "credential_event_log_dropped" in caplog.text
    finally:
        release.set()
    log_runner.shutdown(wait=True)
    assert len(written) == 2
    assert log_runner.status()["accepted"] == 0


def test_diagnostic_failure_cannot_fail_a_committed_exchange(service_factory, monkeypatch):
    import services.account_service as accounts
    old, new = jwt(-3600), jwt(7 * 86400)
    service = service_factory([row(old, refresh_token="test-rt")])
    monkeypatch.setattr(service, "_request_access_token_refresh", lambda *a, **kw: {
        "access_token": new, "refresh_token": "new-test-rt"})
    def fail(*args):
        raise RuntimeError("synthetic logging outage")
    monkeypatch.setattr(accounts.log_service, "add", fail)
    assert service.ensure_access_token(old, raise_on_error=True) == new
    assert service.storage.load_accounts()[0]["refresh_token"] == "new-test-rt"


def test_slow_error_log_does_not_mask_recovery_error_or_backoff(service_factory, monkeypatch, log_runner):
    import services.account_service as accounts
    old = jwt(-3600)
    service = service_factory([row(old, refresh_token="test-rt")])
    def refresh(*args, **kwargs):
        raise accounts.OAuthRefreshError(503, "synthetic_temporary_failure")
    monkeypatch.setattr(service, "_request_access_token_refresh", refresh)
    entered, release = Event(), Event()
    def log(*args):
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(accounts.log_service, "add", log)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(service.ensure_access_token, old, image_scope=True, raise_on_error=True)
        try:
            assert entered.wait(2)
            with pytest.raises(accounts.OAuthRefreshError):
                result.result(timeout=1)
            assert service.storage.load_accounts()[0]["last_token_refresh_error_at"]
            assert not service._is_image_account_available(service._accounts[old])
        finally:
            release.set()
