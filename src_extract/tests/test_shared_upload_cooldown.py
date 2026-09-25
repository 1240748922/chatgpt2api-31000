"""Upload limits must be visible before another replica can reuse a lease."""
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import services.account_service as module
from services.account_service import AccountService, ImageAccountSelectionError
from services.account_capabilities import upload_blocked
from services.credential_coordinator import CredentialBusy, CredentialCoordinator
from services.image_failure import image_failure
from test_account_auth_quarantine import jwt, row, service_factory


@pytest.fixture(autouse=True)
def strict(monkeypatch):
    monkeypatch.setenv("CHATGPT2API_STRICT_IMAGE_CREDENTIALS", "1")


def test_deferred_cooldown_blocks_stale_replica_but_preserves_text_generation(service_factory, monkeypatch):
    token = jwt(7200)
    first = service_factory([row(token, refresh_token="synthetic-rt")])
    second = service_factory(url=first.storage.database_url)
    # Hold the ordinary result writer: shared dispatch protection must not
    # depend on that queue, a snapshot refresh, or a changed account row.
    monkeypatch.setattr(first, "_schedule_image_result_persist", lambda *a: None)
    lease = first.get_available_access_token(requires_file_upload=True)
    first.mark_image_result(lease, False, failure=image_failure("file_upload_throttled", retry_after=120),
                            defer_persistence=True)
    assert not upload_blocked(second.storage.load_accounts()[0])
    with pytest.raises(ImageAccountSelectionError):
        second.get_available_access_token(requires_file_upload=True)
    assert upload_blocked(second.get_account(token, refresh_snapshot=False))
    assert not second._image_inflight
    text = second.get_available_access_token()
    assert text == token
    second.release_image_slot(text)
    assert first.get_account(token, refresh_snapshot=False)["quota"] == 8


def test_stale_replica_continues_to_next_candidate(service_factory, monkeypatch):
    limited, healthy = jwt(7200), jwt(7201)
    first = service_factory([row(limited), row(healthy)])
    second = service_factory(url=first.storage.database_url)
    monkeypatch.setattr(first, "_schedule_image_result_persist", lambda *a: None)
    lease = first.get_available_access_token(requires_file_upload=True)
    assert lease == limited
    first.mark_image_result(lease, False, failure=image_failure("file_upload_throttled"), defer_persistence=True)
    next_lease = second.get_available_access_token(requires_file_upload=True)
    assert next_lease == healthy
    second.release_image_slot(next_lease)


def test_late_success_cannot_clear_shared_cooldown(service_factory, monkeypatch):
    monkeypatch.setattr(module, "config", SimpleNamespace(image_account_concurrency=2, image_request_timeout_secs=300))
    token = jwt(7200)
    first = service_factory([row(token)])
    second = service_factory(url=first.storage.database_url)
    for service in (first, second):
        monkeypatch.setattr(service, "_schedule_image_result_persist", lambda *a: None)
    limited = first.get_available_access_token(requires_file_upload=True)
    in_flight = second.get_available_access_token(requires_file_upload=True)
    first.mark_image_result(limited, False, failure=image_failure("file_upload_throttled"), defer_persistence=True)
    second.mark_image_result(in_flight, True, defer_persistence=True)
    with pytest.raises(CredentialBusy, match="upload"):
        second._credential_coordinator.acquire_image(second._accounts[token], minimum_validity=300,
                                                     duration=400, limit=2, requires_upload=True)


def test_conflicting_result_writers_preserve_longer_cooldown(service_factory, monkeypatch):
    monkeypatch.setattr(module, "config", SimpleNamespace(image_account_concurrency=2, image_request_timeout_secs=300))
    token = jwt(7200)
    first = service_factory([row(token)])
    second = service_factory(url=first.storage.database_url)
    a = first.get_available_access_token(requires_file_upload=True)
    b = second.get_available_access_token(requires_file_upload=True)
    first.mark_image_result(a, False, failure=image_failure("file_upload_throttled", retry_after=3600))
    second.mark_image_result(b, False, failure=image_failure("file_upload_throttled", retry_after=60))
    saved = first.storage.load_accounts()[0]
    assert saved["file_upload_blocked_until"] > time.time() + 3500
    assert saved["quota"] == 8
    assert saved["fail"] == 2


def test_cooldown_merge_does_not_shorten_remote_state():
    merged = AccountService._merge_account_fields(
        {"file_upload_blocked_until": 0}, {"file_upload_blocked_until": 1060},
        {"file_upload_blocked_until": 4600, "quota": 7},
    )
    assert merged["file_upload_blocked_until"] == 4600
    assert merged["quota"] == 7


def test_shared_cooldown_survives_restart_and_duplicate_release(service_factory):
    token = jwt(7200)
    service = service_factory([row(token)])
    lease = service.get_available_access_token(requires_file_upload=True)
    lease.release(upload_cooldown_seconds=120)
    restarted = CredentialCoordinator(service.storage)
    with restarted.edit(lease.key) as (_, data, _):
        until = data["file_upload_blocked_until"]
    # Retrying a cleanup, even with a larger duration, is not a new 429.
    lease.release(upload_cooldown_seconds=3600)
    with restarted.edit(lease.key) as (_, data, _):
        assert data["file_upload_blocked_until"] == until
    with pytest.raises(CredentialBusy, match="upload"):
        restarted.acquire_image(service._accounts[token], minimum_validity=300, duration=400, limit=2,
                                 requires_upload=True)
    # Simulate expiry without waiting or re-uploading to the real upstream.
    with restarted.edit(lease.key) as (_, data, now):
        data["file_upload_blocked_until"] = now - 1
    recovered = restarted.acquire_image(service._accounts[token], minimum_validity=300, duration=400, limit=2,
                                         requires_upload=True)
    recovered.release()
    service.release_image_slot(lease)


def test_failed_shared_publication_rolls_back_release_and_retains_retry(service_factory, monkeypatch):
    token = jwt(7200)
    service = service_factory([row(token)])
    lease = service.get_available_access_token(requires_file_upload=True)
    coordinator = lease.coordinator
    original = coordinator.edit

    @contextmanager
    def cannot_commit(key):
        with original(key) as state:
            yield state
            raise RuntimeError("synthetic commit failure")

    monkeypatch.setattr(coordinator, "edit", cannot_commit)
    with pytest.raises(RuntimeError):
        lease.release(upload_cooldown_seconds=120)
    monkeypatch.setattr(coordinator, "edit", original)
    with coordinator.edit(lease.key) as (_, data, _):
        assert lease.owner in data["leases"]
        assert not upload_blocked(data)
    # Standard cleanup still carries the failed cooldown publication.
    lease.release()
    with coordinator.edit(lease.key) as (_, data, _):
        assert lease.owner not in data["leases"]
        assert upload_blocked(data)
    service.release_image_slot(lease)


def test_shared_cooldown_learned_once_does_not_repeat_db_probes(service_factory, monkeypatch):
    token = jwt(7200)
    first = service_factory([row(token)])
    second = service_factory(url=first.storage.database_url)
    lease = first.get_available_access_token(requires_file_upload=True)
    lease.release(upload_cooldown_seconds=120)
    with pytest.raises(ImageAccountSelectionError):
        second.get_available_access_token(requires_file_upload=True)
    monkeypatch.setattr(second._credential_coordinator, "acquire_image",
                        lambda *a, **kw: pytest.fail("repeated shared probe despite cached cooldown"))
    with pytest.raises(ImageAccountSelectionError):
        second.get_available_access_token(requires_file_upload=True)
    assert second._accounts[token]["quota"] == 8
    assert second._accounts[token].get("fail", 0) == 0
    assert not second._accounts[token].get("last_file_upload_throttled_at")
    first.release_image_slot(lease)


def test_brief_gate_contention_does_not_lose_cooldown(service_factory, monkeypatch):
    token = jwt(7200)
    service = service_factory([row(token)])
    lease = service.get_available_access_token(requires_file_upload=True)
    original = lease.coordinator.release
    calls = []

    def conflict_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise CredentialBusy()
        return original(*args, **kwargs)

    monkeypatch.setattr(lease.coordinator, "release", conflict_once)
    service.mark_image_result(lease, False, failure=image_failure("file_upload_throttled"), defer_persistence=True)
    with lease.coordinator.edit(lease.key) as (_, data, _):
        assert upload_blocked(data)
        assert lease.owner not in data["leases"]


def test_persistent_gate_contention_is_bounded_and_keeps_lease(service_factory, monkeypatch):
    token = jwt(7200)
    service = service_factory([row(token)])
    lease = service.get_available_access_token(requires_file_upload=True)
    original = lease.coordinator.release
    calls = []

    def blocked(*args, **kwargs):
        calls.append(1)
        if len(calls) > 3:
            pytest.fail("unbounded release retry")
        raise CredentialBusy()

    monkeypatch.setattr(lease.coordinator, "release", blocked)
    with pytest.raises(CredentialBusy):
        lease.release(upload_cooldown_seconds=120)
    with lease.coordinator.edit(lease.key) as (_, data, _):
        assert lease.owner in data["leases"]
    monkeypatch.setattr(lease.coordinator, "release", original)
    service.release_image_slot(lease)
    with lease.coordinator.edit(lease.key) as (_, data, _):
        assert upload_blocked(data)


def test_shared_backoff_history_survives_stale_account_row(service_factory, monkeypatch):
    monkeypatch.setenv("CHATGPT2API_UPLOAD_COOLDOWN_SECONDS", "900")
    token = jwt(7200)
    first = service_factory([row(token)])
    second = service_factory(url=first.storage.database_url)
    monkeypatch.setattr(second, "_schedule_image_result_persist", lambda *a: None)
    previous = first.get_available_access_token(requires_file_upload=True)
    previous.release(upload_cooldown_seconds=900, upload_retry_after_missing=True)
    with previous.coordinator.edit(previous.key) as (_, data, now):
        data["file_upload_blocked_until"] = now - 1
    lease = second.get_available_access_token(requires_file_upload=True)
    result = second.mark_image_result(lease, False, failure=image_failure("file_upload_throttled"),
                                      defer_persistence=True)
    assert result["file_upload_throttle_streak"] == 2
    assert result["file_upload_blocked_until"] > time.time() + 1790
    assert result["quota"] == 8
    first.release_image_slot(previous)


def test_cooldown_conflict_keeps_matching_backoff_history():
    remote = {"file_upload_blocked_until": 4600, "file_upload_throttle_streak": 3}
    local = {"file_upload_blocked_until": 1060, "file_upload_throttle_streak": 1}
    merged = AccountService._merge_account_fields({}, local, remote)
    assert merged["file_upload_blocked_until"] == 4600
    assert merged["file_upload_throttle_streak"] == 3


def test_legacy_long_cooldown_does_not_inherit_unrelated_streak():
    merged = AccountService._merge_account_fields(
        {}, {"file_upload_blocked_until": 1060, "file_upload_throttle_streak": 4},
        {"file_upload_blocked_until": 4600},
    )
    assert merged["file_upload_blocked_until"] == 4600
    assert not merged.get("file_upload_throttle_streak")
