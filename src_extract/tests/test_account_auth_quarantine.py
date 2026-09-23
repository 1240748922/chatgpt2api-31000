"""Expired/rejected credentials stay out of image dispatch until recovery.

Only synthetic tokens and temporary SQLite databases; no upstream requests.
"""
import base64
import json
import time
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone

import pytest

import services.account_service as module
from services.account_service import AccountService, OAuthRefreshError
from services.application_database import dispose_database_engine
from services.image_failure import image_failure
from services.storage.database_storage import DatabaseStorageBackend


def jwt(lifetime):
    payload = {"exp": int(time.time()) + lifetime, "sub": "synthetic"}
    return "e30." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".test"


def now():
    return datetime.now(timezone.utc).isoformat()


def row(token, **kwargs):
    return {"access_token": token, "status": "正常", "quota": 8,
            "image_quota_unknown": False, "last_remote_checked_at": now(), **kwargs}


@pytest.fixture
def service_factory(tmp_path, monkeypatch):
    services = []
    monkeypatch.setattr(AccountService, "_load_cumulative_total", lambda self: len(self._accounts))
    monkeypatch.setattr(module.log_service, "add", lambda *a, **kw: None)

    def create(rows=None, url=None):
        url = url or "sqlite:///" + str(tmp_path / f"accounts-{len(services)}.sqlite")
        storage = DatabaseStorageBackend(url)
        if rows is not None:
            storage.replace_accounts(rows)
        service = AccountService(storage)
        service._image_shard_count = 1
        service._ACCOUNT_SNAPSHOT_TTL_SECONDS = 3600
        service._IMAGE_POOL_WAIT_SECONDS = 0
        monkeypatch.setattr(service, "_schedule_image_failure_refresh_async", lambda *a, **kw: None)
        monkeypatch.setattr(service, "_start_pending_image_failure_refreshes", lambda: None)
        monkeypatch.setattr(service, "_request_access_token_refresh",
                            lambda *a, **kw: pytest.fail("unexpected OAuth request"))
        services.append((service, url))
        return service

    yield create
    for service, url in services:
        service._image_result_persist_executor.shutdown(wait=True)
        service._image_failure_schedule_executor.shutdown(wait=True)
        dispose_database_engine(url)


@pytest.mark.parametrize("status", ["异常", "禁用"])
def test_explicitly_abnormal_or_disabled_stays_excluded(status):
    assert not AccountService._is_image_account_available(row(jwt(7 * 86400), status=status))


def test_expired_without_rt_stays_excluded():
    assert not AccountService._is_image_account_available(row(jwt(-3600)))


def test_expired_at_in_refresh_backoff_is_skipped(service_factory):
    expired = jwt(-3600)
    service = service_factory([
        row(expired, refresh_token="synthetic-rt", last_token_refresh_error_at=now()),
        row("healthy-test", image_quota_unknown=True),
    ])
    assert service.get_available_access_token() == "healthy-test"
    service.release_image_slot("healthy-test")
    assert service._image_inflight == {}


def test_refresh_failure_does_not_poison_the_next_request(service_factory, monkeypatch):
    expired = jwt(-3600)
    service = service_factory([row(expired, refresh_token="synthetic-rt"),
                               row("healthy-test", image_quota_unknown=True)])
    calls = []

    def temporary_failure(*a, **kw):
        calls.append(1)
        raise OAuthRefreshError(503, "temporarily_unavailable", "synthetic outage")

    monkeypatch.setattr(service, "_request_access_token_refresh", temporary_failure)
    for _ in range(3):
        assert service.get_available_access_token() == "healthy-test"
        service.release_image_slot("healthy-test")
    assert calls == [1]
    assert service._image_inflight == {}
    assert service.get_account(expired)["status"] == "正常"  # Not permanently disabled/deleted.


@pytest.mark.parametrize("busy", [False, True])
def test_ensure_rejects_expired_at_during_backoff(service_factory, busy):
    expired = jwt(-3600)
    service = service_factory([row(expired, refresh_token="synthetic-rt", last_token_refresh_error_at=now())])
    service._image_inflight[expired] = 2 if busy else 0
    with pytest.raises(OAuthRefreshError):
        service.ensure_access_token(expired, skip_if_image_busy=busy)


def test_unexpired_at_remains_usable_during_refresh_backoff(service_factory):
    token = jwt(3600)
    service = service_factory([row(token, refresh_token="synthetic-rt", last_token_refresh_error_at=now())])
    assert service.ensure_access_token(token) == token
    assert service.get_available_access_token() == token
    service.release_image_slot(token)


def test_expired_without_rt_never_returned_by_ensure(service_factory):
    token = jwt(-3600)
    service = service_factory([row(token)])
    with pytest.raises(OAuthRefreshError):
        service.ensure_access_token(token)


def test_remote_invalid_at_is_refreshed_even_before_local_expiry(service_factory, monkeypatch):
    token, new = jwt(7 * 86400), jwt(8 * 86400)
    service = service_factory([row(token, refresh_token="old-rt", last_remote_check_result="invalid")])
    monkeypatch.setattr(service, "_request_access_token_refresh",
                        lambda *a, **kw: {"access_token": new, "refresh_token": "new-rt"})
    assert service.ensure_access_token(token) == new


def test_text_selection_skips_credential_rejected_during_maintenance(service_factory, monkeypatch):
    service = service_factory([row("rejected"), row("healthy")])
    def maintain(token, **kw):
        if token == "rejected":
            raise OAuthRefreshError(503, "access_token_unavailable")
        return token
    monkeypatch.setattr(service, "ensure_access_token", maintain)
    assert service.get_text_access_token() == "healthy"


def test_nonraising_refresh_fallback_still_rejects_expired_at(service_factory, monkeypatch):
    token = jwt(-3600)
    service = service_factory([row(token, refresh_token="synthetic-rt")])
    def outage(*a, **kw):
        raise OAuthRefreshError(503, "temporary")
    monkeypatch.setattr(service, "_request_access_token_refresh", outage)
    with pytest.raises(OAuthRefreshError):
        service.ensure_access_token(token, raise_on_error=False)


def test_refresh_resumes_after_backoff_and_rotates_lease(service_factory, monkeypatch):
    old, new = jwt(-3600), jwt(7 * 86400)
    service = service_factory([row(old, refresh_token="old-rt", last_token_refresh_error_at=(
        datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat())])
    monkeypatch.setattr(service, "_request_access_token_refresh",
                        lambda *a, **kw: {"access_token": new, "refresh_token": "new-rt"})
    assert service.get_available_access_token() == new
    service.release_image_slot(old)
    assert service._image_inflight == {}
    assert not service.get_account(new)["last_token_refresh_error_at"]


def test_singleflight_can_finish_recovery_while_backoff_is_present(service_factory, monkeypatch):
    old, new = jwt(-3600), jwt(7 * 86400)
    service = service_factory([row(old, refresh_token="old-rt", last_token_refresh_error_at=now())])
    account = service.get_account(old)
    future = Future()
    service._oauth_refresh_flights[service._credential_generation(old, account)] = future
    service._accounts[new] = row(new)
    future.set_result(new)
    assert service.ensure_access_token(old, deadline_monotonic=time.monotonic() + 1) == new


def test_lease_rechecked_after_maintenance_not_dispatched_if_quarantined(service_factory, monkeypatch):
    service = service_factory([row("rejected"), row("healthy")])
    def maintain(token, **kw):
        if token == "rejected":
            service.mark_image_result(token, False, failure=image_failure("auth_invalid"))
        return token
    monkeypatch.setattr(service, "ensure_access_token", maintain)
    assert service.get_available_access_token() == "healthy"
    service.release_image_slot("healthy")
    assert service._image_inflight == {}


def pending_service(service_factory, token="rejected"):
    service = service_factory([row(token, refresh_token="synthetic-rt")])
    service.mark_image_result(token, False, failure=image_failure("auth_invalid"))
    return service


def test_verification_timeout_keeps_quarantine_without_immediate_retry(service_factory, monkeypatch):
    service = pending_service(service_factory)
    before = service.get_account("rejected")
    from services import openai_backend_api
    class OfflineBackend:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get_user_info(self): raise TimeoutError("synthetic verification timeout")
    monkeypatch.setattr(openai_backend_api, "OpenAIBackendAPI", OfflineBackend)
    service._verify_pending_auth("rejected", "image_failure")
    after = service.get_account("rejected")
    assert after["last_remote_check_result"] == "pending"
    assert after["pending_auth_verification_id"] == before["pending_auth_verification_id"]
    assert after["pending_auth_scope"] == "image"
    assert after["last_remote_check_error"]
    assert not service._is_image_account_available(after)
    assert not service._image_failure_refresh_pending
    assert not service._schedule_account_refresh_after_image_failure("rejected", force=True)


@pytest.mark.parametrize("scope", ["image", "account"])
def test_quarantine_and_retry_backoff_survive_restart(service_factory, scope):
    service = service_factory([row("rejected")])
    service.schedule_auth_verification("rejected", "test", scope=scope, remove_invalid=False)
    service._record_remote_check_error("rejected", "test", "temporary")
    replica = service_factory(url=str(service.storage.engine.url))
    item = replica.get_account("rejected")
    assert item["last_remote_check_result"] == "pending"
    assert item["pending_auth_scope"] == scope
    assert item["pending_auth_remove_invalid"] is False
    assert not replica._is_image_account_available(item)
    assert replica.resume_pending_auth_verifications(limit=2) == 0


def test_due_retry_not_starved_by_first_rows_in_backoff(service_factory):
    service = service_factory([row("cooling"), row("due")])
    for token in ("cooling", "due"):
        service.mark_image_result(token, False, failure=image_failure("auth_invalid"))
    service._record_remote_check_error("cooling", "test", "temporary")
    assert service.resume_pending_auth_verifications(limit=1) == 1
    assert list(service._image_failure_refresh_pending) == ["due"]


def test_auth_retry_resumes_after_persisted_backoff(service_factory):
    service = pending_service(service_factory)
    service._record_remote_check_error("rejected", "test", "temporary")
    service.update_account("rejected", {"last_remote_check_error_at": (
        datetime.now(timezone.utc) - timedelta(seconds=31)).isoformat()})
    assert service.resume_pending_auth_verifications(limit=1) == 1
    assert list(service._image_failure_refresh_pending) == ["rejected"]


def test_refresh_failure_backoff_also_bounds_background_verification(service_factory):
    service = pending_service(service_factory)
    service._record_token_refresh_error("rejected", "test", "temporary")
    service._record_remote_check_error("rejected", "test", "temporary")
    service.update_account("rejected", {"last_remote_check_error_at": (
        datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()})
    assert service.resume_pending_auth_verifications(limit=1) == 0


def test_ordinary_remote_error_does_not_quarantine_healthy_account(service_factory):
    service = service_factory([row("healthy")])
    service._record_remote_check_error("healthy", "quota_sync", "temporary")
    assert service.get_available_access_token() == "healthy"
    service.release_image_slot("healthy")


def test_confirmed_verification_success_releases_quarantine(service_factory):
    service = pending_service(service_factory)
    service._record_remote_check_error("rejected", "test", "temporary")
    item = service.get_account("rejected")
    service._record_refresh_success("rejected", {"status": "正常", "quota": 8}, "test",
        expected_access_token="rejected", expected_refresh_token="synthetic-rt",
        expected_remote_check_marker=service._remote_check_marker(item))
    assert service.get_account("rejected")["last_remote_check_result"] == "ok"
    assert service.get_available_access_token() == "rejected"
    service.release_image_slot("rejected")


@pytest.mark.parametrize("scope", ["image", "account"])
def test_pending_expired_at_without_rt_can_still_be_verified(service_factory, monkeypatch, scope):
    from services import openai_backend_api
    token = jwt(-3600)
    service = service_factory([row(token)])
    service.schedule_auth_verification(token, "test", scope=scope, remove_invalid=False)
    class OfflineBackend:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get_user_info(self): raise openai_backend_api.InvalidAccessTokenError("synthetic expired AT")
    monkeypatch.setattr(openai_backend_api, "OpenAIBackendAPI", OfflineBackend)
    service._refresh_account_after_image_failure(token)
    item = service.get_account(token)
    assert item["last_remote_check_result"] == "invalid"
    assert item["status"] == "异常"
    assert not service._is_image_account_available(item)


def test_stale_verification_failure_does_not_quarantine_new_credentials(service_factory):
    service = pending_service(service_factory)
    before = service.get_account("rejected")
    marker = service._remote_check_marker(before)
    new = jwt(7 * 86400)
    service.update_account("rejected", {"access_token": new, "refresh_token": "new-rt",
                                       "last_remote_check_result": "ok"})
    assert not service._record_remote_check_error("rejected", "test", "stale error",
        expected_access_token="rejected", expected_refresh_token="synthetic-rt",
        expected_remote_check_marker=marker)
    assert service.get_available_access_token() == new
    service.release_image_slot(new)


def test_pending_state_survives_another_replica_snapshot(service_factory):
    service = pending_service(service_factory)
    service._record_remote_check_error("rejected", "test", "temporary")
    other = service_factory(url=str(service.storage.engine.url))
    other.update_account("rejected", {"name": "renamed elsewhere"})
    service._account_snapshot_checked_at = 0
    assert service._refresh_accounts_snapshot_if_stale(wait_for_refresh=True)
    item = service.get_account("rejected", refresh_snapshot=False)
    assert item["name"] == "renamed elsewhere"
    assert item["last_remote_check_result"] == "pending"
    assert not service._is_image_account_available(item)


def test_failed_background_worker_does_not_rerun_in_a_tight_loop(service_factory, monkeypatch):
    from threading import Event
    service = pending_service(service_factory)
    service._image_failure_refresh_pending.append("rejected")
    service._image_failure_refresh_pending_set.add("rejected")
    service._image_failure_refresh_pending_scopes["rejected"] = "image"
    service._image_failure_refresh_rerun.add("rejected")
    finished = Event()
    calls = []
    class InlineThread:
        def __init__(self, *, target, **kwargs): self.target = target
        def start(self): self.target()
    def verify(token):
        calls.append(token)
        assert len(calls) == 1, "unbounded retry after a verification outage"
        service._record_remote_check_error(token, "test", "temporary")
        finished.set()
    monkeypatch.setattr(module, "Thread", InlineThread)
    monkeypatch.setattr(service, "_refresh_account_after_image_failure", verify)
    monkeypatch.setattr(service, "_start_pending_image_failure_refreshes",
                        lambda: AccountService._start_pending_image_failure_refreshes(service))
    service._start_pending_image_failure_refreshes()
    assert finished.is_set() and calls == ["rejected"]
    assert not service._image_failure_refresh_pending
    assert not service._image_failure_refresh_active


def test_generation_skips_expired_pool_without_spending_upstream_attempts(service_factory, monkeypatch):
    from types import SimpleNamespace
    from services.protocol import conversation as protocol
    stale = [row(jwt(-3600 - i), refresh_token="rt-test", last_token_refresh_error_at=now()) for i in range(4)]
    service = service_factory(stale + [row("healthy-test", image_quota_unknown=True)])
    selected = []
    class Backend:
        def __init__(self, access_token, **kwargs):
            selected.append(access_token)
            self.proxy_profile = SimpleNamespace()
        def close(self): pass
    monkeypatch.setattr(protocol, "account_service", service)
    monkeypatch.setattr(protocol, "OpenAIBackendAPI", Backend)
    monkeypatch.setattr(protocol, "config", SimpleNamespace(
        image_account_retry_enabled=True, image_max_account_attempts=4, image_stream_timeout_secs=90))
    monkeypatch.setattr(protocol, "proxy_settings", SimpleNamespace(
        acquire_image_egress=lambda *a, **kw: 0, get_fallback_proxy_reference=lambda: ""))
    monkeypatch.setattr(protocol, "_cleanup_image_conversations_after_success", lambda *a: None)
    def output(backend, request, index, total):
        assert selected[-1] == "healthy-test", "Expired credentials reached the upstream boundary"
        yield protocol.ImageOutput(kind="result", model=request.model, index=index,
                                   total=total, data=[{"url": "/images/synthetic.png"}])
    monkeypatch.setattr(protocol, "stream_image_outputs", output)
    outputs = protocol._generate_single_image(protocol.ConversationRequest(model="gpt-image-2"), 1, 1)
    assert outputs[0].kind == "result"
    assert len(outputs[0].image_attempts) == 1
    assert selected == ["healthy-test"]
    assert service._image_inflight == {}
