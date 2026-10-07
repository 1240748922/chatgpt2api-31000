"""Strict dispatch must survive an expired shard without bypassing shared leases."""
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from test_account_auth_quarantine import jwt, row, service_factory
from services.account_service import ImageAccountSelectionError
from services.credential_coordinator import CredentialBusy, LeasedToken
from services.runtime_configuration import stable_shard_index


def account(shard, name, lifetime=7200, **fields):
    for suffix in range(1000):
        candidate = "acct_" + hashlib.sha256(f"{name}:{suffix}".encode()).hexdigest()[:24]
        if stable_shard_index(candidate, 8) == shard:
            return row(jwt(lifetime) + name, management_id=candidate, **fields)
    raise AssertionError("synthetic shard ID not found")


def configure(service, index=3):
    service._image_shard_count = 8
    service._image_shard_index = index
    service._IMAGE_POOL_WAIT_SECONDS = 0
    return service


@pytest.fixture(autouse=True)
def strict(monkeypatch):
    monkeypatch.setenv("CHATGPT2API_STRICT_IMAGE_CREDENTIALS", "1")


def test_expired_local_shard_borrows_a_ready_account(service_factory):
    expired = account(3, "expired", -3600, refresh_token="synthetic-rt")
    ready = account(0, "ready")
    service = configure(service_factory([expired, ready]))
    lease = service.get_available_access_token(requires_file_upload=True)
    assert isinstance(lease, LeasedToken)
    assert lease == ready["access_token"]
    diagnostics = service.get_image_selection_diagnostics()
    assert diagnostics["account_shard_fallback"] == 1
    assert diagnostics["account_selected_shard_index"] == 0
    assert diagnostics["local_ready_count"] == 0
    assert diagnostics["local_matched_count"] == 1
    service.release_image_slot(lease)
    assert service._image_inflight == {}


def test_shard_monitor_does_not_count_expired_recoverable_tokens_as_ready(service_factory):
    service = configure(service_factory([
        account(3, "expired", -3600, refresh_token="synthetic-rt"),
        account(3, "ready"), account(0, "elsewhere"),
    ]))
    snapshot = service.image_shard_snapshot()
    assert snapshot["assigned_accounts"] == 2
    assert snapshot["ready_accounts"] == 1


def test_local_candidate_keeps_fast_path_and_priority(service_factory, monkeypatch):
    remote = account(0, "funded")
    local = account(3, "unknown", image_quota_unknown=True, quota=0)
    service = configure(service_factory([remote, local]))
    original = service._find_available_image_token_locked
    def local_only(*args, **kwargs):
        assert not kwargs.get("other_shards")
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "_find_available_image_token_locked", local_only)
    lease = service.get_available_access_token()
    assert lease == local["access_token"]
    assert service.get_image_selection_diagnostics()["account_shard_fallback"] == 0
    service.release_image_slot(lease)


def test_busy_local_slot_can_borrow_but_prewarm_stays_local(service_factory):
    local, remote = account(3, "local"), account(0, "remote")
    service = configure(service_factory([local, remote]))
    first = service.get_available_access_token()
    assert first == local["access_token"]
    assert service.page_prewarm_candidate(set()) is None
    second = service.get_available_access_token()
    assert second == remote["access_token"]
    assert service.get_image_selection_diagnostics()["local_busy_count"] == 1
    service.release_image_slot(first)
    service.release_image_slot(second)


@pytest.mark.parametrize("changes,kwargs", [
    ({"lifetime": -30, "refresh_token": "synthetic-rt"}, {}),
    ({"lifetime": 200}, {}),
    ({"status": "禁用"}, {}),
    ({"status": "异常"}, {}),
    ({"status": "限流", "quota": 0}, {}),
    ({"last_remote_check_result": "pending", "pending_auth_scope": "image"}, {}),
    ({"type": "free"}, {"plan_type": "plus"}),
    ({"source_type": "web"}, {"source_type": "codex"}),
    ({"file_upload_blocked_until": time.time() + 3600}, {"requires_file_upload": True}),
])
def test_fallback_keeps_all_eligibility_filters(service_factory, changes, kwargs):
    remote = account(0, "ineligible", **changes)
    service = configure(service_factory([remote]))
    with pytest.raises(ImageAccountSelectionError):
        service.get_available_access_token(**kwargs)
    assert service._image_inflight == {}


def test_excluded_account_is_not_reintroduced_by_fallback(service_factory):
    remote = account(0, "excluded")
    service = configure(service_factory([remote]))
    with pytest.raises(ImageAccountSelectionError):
        service.get_available_access_token(excluded_tokens={remote["access_token"]})
    assert not service._image_inflight


def test_shared_upload_cooldown_excludes_borrower_but_allows_text(service_factory):
    remote = account(0, "upload-limited")
    owner = configure(service_factory([remote]), 0)
    borrower = configure(service_factory(url=owner.storage.database_url), 3)
    lease = owner.get_available_access_token(requires_file_upload=True)
    lease.release(upload_cooldown_seconds=120)
    owner.release_image_slot(lease)
    with pytest.raises(ImageAccountSelectionError):
        borrower.get_available_access_token(requires_file_upload=True)
    assert not borrower._image_inflight
    text_lease = borrower.get_available_access_token()
    assert text_lease == remote["access_token"]
    borrower.release_image_slot(text_lease)


def test_exhausted_local_quota_does_not_hide_other_shard_capacity(service_factory):
    local = account(3, "limited", status="限流", quota=0)
    remote = account(0, "funded")
    service = configure(service_factory([local, remote]))
    lease = service.get_available_access_token()
    assert lease == remote["access_token"]
    diagnostics = service.get_image_selection_diagnostics()
    assert diagnostics["limited_count"] == 1
    assert diagnostics["matched_count"] == 2
    service.release_image_slot(lease)


def test_all_shards_quota_limited_retains_quota_error(service_factory):
    service = configure(service_factory([
        account(3, "local-limited", status="限流", quota=0),
        account(0, "remote-limited", status="限流", quota=0),
    ]))
    with pytest.raises(ImageAccountSelectionError) as raised:
        service.get_available_access_token()
    assert raised.value.kind == "quota_exhausted"
    assert service.get_image_selection_diagnostics()["matched_count"] == 2


@pytest.mark.parametrize("unprotected", ["non_strict", "no_coordinator"])
def test_fallback_never_uses_uncoordinated_slots(service_factory, monkeypatch, unprotected):
    service = configure(service_factory([account(0, "remote")]))
    if unprotected == "non_strict":
        monkeypatch.setenv("CHATGPT2API_STRICT_IMAGE_CREDENTIALS", "0")
    else:
        service._credential_coordinator = None
    with pytest.raises(ImageAccountSelectionError):
        service.get_available_access_token()
    assert not service._image_inflight


def test_owner_and_borrower_share_one_lease_and_refresh_fence(service_factory):
    remote = account(0, "remote", refresh_token="synthetic-rt")
    owner = configure(service_factory([remote]), 0)
    borrower = configure(service_factory(url=owner.storage.database_url), 3)
    first = owner.get_available_access_token()
    with pytest.raises(ImageAccountSelectionError):
        borrower.get_available_access_token()
    assert not borrower._image_inflight
    owner.release_image_slot(first)
    # Expire the one-second local contention hint without changing the DB gate.
    borrower._credential_blocked.clear()
    second = borrower.get_available_access_token()
    with pytest.raises(CredentialBusy):
        owner._credential_coordinator.begin_refresh(owner._accounts[first])
    borrower.release_image_slot(second)
    ticket = owner._credential_coordinator.begin_refresh(owner._accounts[first])
    with pytest.raises(ImageAccountSelectionError):
        borrower.get_available_access_token()
    owner._credential_coordinator.finish_refresh(ticket, "uncertain")
    borrower._credential_blocked.clear()
    with pytest.raises(ImageAccountSelectionError):
        borrower.get_available_access_token()
    assert not borrower._image_inflight


def test_simultaneous_empty_shards_cannot_double_lease(service_factory):
    first = configure(service_factory([account(0, "remote")]), 3)
    second = configure(service_factory(url=first.storage.database_url), 6)
    barrier = Barrier(2)
    def acquire(service):
        barrier.wait(timeout=5)
        try:
            return service, service.get_available_access_token()
        except ImageAccountSelectionError:
            return service, None
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(acquire, service) for service in (first, second)]
        results = [future.result(timeout=10) for future in futures]
    winners = [(service, lease) for service, lease in results if lease is not None]
    assert len(winners) == 1
    winners[0][0].release_image_slot(winners[0][1])
    assert not first._image_inflight and not second._image_inflight


def test_fallback_respects_deadline_and_releases_late_shared_lease(service_factory, monkeypatch):
    service = configure(service_factory([account(0, "remote")]))
    original = service._credential_coordinator.acquire_image
    def delayed(*args, **kwargs):
        lease = original(*args, **kwargs)
        time.sleep(.1)
        return lease
    monkeypatch.setattr(service._credential_coordinator, "acquire_image", delayed)
    with pytest.raises(ImageAccountSelectionError) as raised:
        service.get_available_access_token(deadline_monotonic=time.monotonic() + .05)
    assert raised.value.kind == "deadline_exceeded"
    assert not service._image_inflight
    monkeypatch.setattr(service._credential_coordinator, "acquire_image", original)
    lease = service.get_available_access_token()
    service.release_image_slot(lease)
