"""Quota fences survive contradictory sync, persistence and concurrent writers."""
import time
from copy import deepcopy

import pytest

import services.image_quota_cooldown as cooldown
from services.account_service import AccountService, ImageAccountSelectionError
from services.account_view import account_row
from services.credential_coordinator import CredentialBusy
from services.image_failure import image_failure
from test_account_auth_quarantine import jwt, row, service_factory


@pytest.fixture(autouse=True)
def strict(monkeypatch):
    monkeypatch.setenv("CHATGPT2API_STRICT_IMAGE_CREDENTIALS", "1")


def check(service, token, **updates):
    previous = service.get_account(token, refresh_snapshot=False)
    return service._record_refresh_success(
        token, {"status": "正常", "quota": 3, "image_quota_unknown": False, **updates},
        expected_access_token=token, expected_refresh_token=previous.get("refresh_token") or "",
        expected_remote_check_marker=service._remote_check_marker(previous),
    )


def exhausted(service_factory):
    token = jwt(7200)
    service = service_factory([row(token)])
    lease = service.get_available_access_token()
    service.mark_image_result(lease, False, failure=image_failure("image_quota_exhausted"))
    return service, token


def test_restart_keeps_contradictory_quota_fenced_then_background_recovers(service_factory):
    service, token = exhausted(service_factory)
    check(service, token)
    replica = service_factory(url=service.storage.database_url)
    assert token not in replica.list_limited_tokens()  # Too early to poll recovery.
    assert replica.evaluate_account_pool()["current_quota"] == 0
    with pytest.raises(ImageAccountSelectionError):
        replica.get_available_access_token()
    replica.update_account(token, {"image_quota_cooldown_until": time.time() - 1})
    assert replica.list_limited_tokens() == [token]  # Raw status is normal, still discoverable.
    with pytest.raises(ImageAccountSelectionError):
        replica.get_available_access_token()
    check(replica, token)
    restored = service_factory(url=service.storage.database_url)
    assert restored.list_limited_tokens() == []
    lease = restored.get_available_access_token()
    assert lease == token
    restored.release_image_slot(lease)


@pytest.mark.parametrize("updates", [
    {"quota": 0, "status": "限流"},
    {"quota": 0, "image_quota_unknown": True},
    {"quota": 3, "status": "禁用"},
])
def test_zero_unknown_or_disabled_check_cannot_recover(service_factory, updates):
    service, token = exhausted(service_factory)
    service.update_account(token, {"image_quota_cooldown_until": time.time() - 1})
    result = check(service, token, **updates)
    assert cooldown.image_quota_needs_verification(result)
    assert not service.is_image_account_available(result)


def test_unlimited_plan_can_recover_after_successful_check(service_factory):
    service, token = exhausted(service_factory)
    service.update_account(token, {"image_quota_cooldown_until": time.time() - 1})
    result = check(service, token, type="pro", quota=0, image_quota_unknown=True)
    assert service.is_image_account_available(result)


def test_transport_error_preserves_recovery_and_manual_enable_cannot_bypass(service_factory):
    service, token = exhausted(service_factory)
    service.update_account(token, {"image_quota_cooldown_until": time.time() - 1, "status": "正常"})
    service._record_remote_check_error(token, "sync_accounts_and_quota", "synthetic timeout")
    assert not service.is_image_account_available(service.get_account(token))
    assert service.list_pending_auth_verification_tokens() == []  # Existing transport backoff still applies.
    service.update_account(token, {"last_remote_check_error_at": "2000-01-01T00:00:00+00:00"})
    assert service.list_pending_auth_verification_tokens() == [token]


def test_shared_lease_rejects_stale_ready_snapshot(service_factory):
    token = jwt(7200)
    service = service_factory([row(token)])
    stale = service.get_account(token)
    lease = service.get_available_access_token()
    service.mark_image_result(lease, False, failure=image_failure("image_quota_exhausted"))
    check(service, token)
    with pytest.raises(CredentialBusy, match="credential_not_ready"):
        service._credential_coordinator.acquire_image(stale, minimum_validity=300, duration=600, limit=2)


def test_deferred_write_still_blocks_other_replica_and_duplicate_release_is_idempotent(service_factory, monkeypatch):
    token = jwt(7200)
    first = service_factory([row(token)])
    second = service_factory(url=first.storage.database_url)
    monkeypatch.setattr(first, "_schedule_image_result_persist", lambda *args: None)
    lease = first.get_available_access_token()
    first.mark_image_result(lease, False, failure=image_failure("image_quota_exhausted"), defer_persistence=True)
    with pytest.raises(CredentialBusy, match="credential_not_ready"):
        second._credential_coordinator.acquire_image(second.get_account(token), minimum_validity=300, duration=600, limit=2)
    before = lease.release()
    after = lease.release(quota_exhausted=True, quota_retry_after=3600)
    assert before == after


def test_recovery_releases_shared_quota_gate(service_factory):
    service, token = exhausted(service_factory)
    service.update_account(token, {"image_quota_cooldown_until": time.time() - 1})
    check(service, token)
    lease = service.get_available_access_token()
    service.mark_image_result(lease, True)
    with lease.coordinator.edit(lease.key) as (_, state, _):
        assert not cooldown.image_quota_needs_verification(state)


def test_success_observation_wins_over_old_cooldown_and_new_failure_wins_back():
    now = time.time()
    baseline = row(jwt(7200))
    cooldown.record_image_quota_exhaustion(baseline, None, now - 20)
    success = deepcopy(baseline)
    cooldown.clear_image_quota_cooldown(success, success=True)
    stale = {**baseline, "email": "updated@example.test"}
    for local, remote in [(success, stale), (stale, success)]:
        result = AccountService._merge_account_fields(baseline, local, remote)
        assert not cooldown.image_quota_needs_verification(result)
    newer = deepcopy(baseline)
    cooldown.record_image_quota_exhaustion(newer, 60, now + 2)
    for local, remote in [(success, newer), (newer, success)]:
        result = AccountService._merge_account_fields(baseline, local, remote)
        assert cooldown.image_quota_needs_verification(result)


def test_concurrent_failures_keep_longest_deadline_and_expired_marker():
    now = time.time()
    short, long = {}, {}
    cooldown.record_image_quota_exhaustion(short, 30, now - 120)
    cooldown.record_image_quota_exhaustion(long, 60, now - 120)
    for local, remote in [(short, long), (long, short)]:
        result = AccountService._merge_account_fields({}, local, remote)
        assert cooldown.image_quota_cooldown_until(result) == now - 60
        assert cooldown.image_quota_needs_verification(result)


def test_repeated_exhaustion_backs_off_even_after_positive_sync(monkeypatch):
    monkeypatch.setenv("CHATGPT2API_IMAGE_QUOTA_COOLDOWN_SECONDS", "300")
    account = {}
    now = time.time()
    cooldown.record_image_quota_exhaustion(account, None, now - 301)
    cooldown.clear_image_quota_cooldown(account)
    cooldown.record_image_quota_exhaustion(account, None, now + 1)
    assert account["image_quota_exhausted_streak"] == 2
    assert account["image_quota_cooldown_until"] == now + 601


def test_pool_health_excludes_expired_recoverable_and_cooling_accounts(service_factory):
    now = time.time()
    cooling = row(jwt(7400))
    cooldown.record_image_quota_exhaustion(cooling, None, now)
    service = service_factory([
        row(jwt(7200), quota=5),
        row(jwt(7300), quota=7, file_upload_blocked_until=now + 300),
        row(jwt(-100), quota=100, refresh_token="synthetic-rt"),
        row("opaque", quota=100, refresh_token="synthetic-rt"),
        row(jwt(20), quota=100), cooling,
    ])
    metrics = service.evaluate_account_pool()
    assert metrics["current_available"] == 2
    assert metrics["current_quota"] == 12
    assert metrics["current_edit_available"] == 1
    assert metrics["current_edit_quota"] == 5


@pytest.mark.parametrize("bad", ["bad", float("inf"), {}, None])
def test_view_handles_malformed_diagnostic_counters(bad):
    account = row(jwt(7200), image_quota_exhausted_streak=bad, image_quota_last_remote_remaining=bad)
    result = account_row(account, available=True, unlimited_quota=False)
    assert result["image_quota_exhausted_streak"] == 0
    assert result["image_quota_last_remote_remaining"] is None


def test_view_explains_quota_fence_with_positive_remote_quota():
    account = row(jwt(7200))
    cooldown.record_image_quota_exhaustion(account, 300, time.time())
    result = account_row(account, available=False, unlimited_quota=False)
    assert result["status_reason_code"] == "image_quota_cooldown"
    account["image_quota_cooldown_until"] = time.time() - 1
    result = account_row(account, available=False, unlimited_quota=False)
    assert result["status_reason_code"] == "image_quota_verification_pending"
