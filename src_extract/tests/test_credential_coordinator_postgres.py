"""CI-only real PostgreSQL interleavings, in a dedicated disposable database."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from threading import Barrier, Event
from contextlib import contextmanager
import time

import pytest
from sqlalchemy import text, make_url

from services.credential_coordinator import CredentialCoordinator, CredentialBusy, CredentialGate
from services.storage.database_storage import DatabaseStorageBackend
from services.storage.base import StorageMutation, StorageRevisionConflictError


@pytest.fixture
def pair():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("requires disposable TEST_POSTGRES_URL")
    assert make_url(url).database == "credential_test", "Never run against an application database"
    store = DatabaseStorageBackend(url)
    with store.engine.begin() as connection:
        connection.execute(text("DELETE FROM credential_runtime_gates"))
        connection.execute(text("DELETE FROM accounts"))
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 3600}).encode()).decode().rstrip("=")
    account = {"access_token": f"e30.{payload}.synthetic", "refresh_token": "synthetic-rt", "status": "正常"}
    store.replace_accounts([account])
    yield CredentialCoordinator(store), CredentialCoordinator(DatabaseStorageBackend(url)), account


def test_postgres_refresh_vs_image_only_one_can_win(pair):
    a, b, account = pair
    barrier = Barrier(2)
    def image():
        barrier.wait()
        try:
            return a.acquire_image(account, minimum_validity=300, duration=400, limit=1)
        except CredentialBusy:
            return None
    def refresh():
        barrier.wait()
        try:
            return b.begin_refresh(account)
        except CredentialBusy:
            return None
    with ThreadPoolExecutor(2) as executor:
        x, y = executor.submit(image), executor.submit(refresh)
        results = [x.result(timeout=5), y.result(timeout=5)]
    assert sum(result is not None for result in results) == 1


def test_postgres_send_fence_survives_new_coordinator(pair):
    a, b, account = pair
    ticket = a.begin_refresh(account)
    with pytest.raises(CredentialBusy):
        b.begin_refresh(account)
    a.finish_refresh(ticket, "uncertain")
    restarted = CredentialCoordinator(a.storage)
    with pytest.raises(CredentialBusy, match="uncertain"):
        restarted.begin_refresh(account)


def test_postgres_no_connection_or_row_lock_held_during_network(pair):
    a, b, account = pair
    ticket = a.begin_refresh(account)
    with b.edit("unrelated-test-key") as (_, data, _):
        data["value"] = 1
    # Lease acquisition finishes quickly rather than blocking on OAuth.
    started = time.monotonic()
    with pytest.raises(CredentialBusy):
        b.acquire_image(account, minimum_validity=300, duration=400, limit=1)
    assert time.monotonic() - started < 2
    a.finish_refresh(ticket, "done")


def test_postgres_exact_release_and_authoritative_generation(pair):
    a, b, account = pair
    one = a.acquire_image(account, minimum_validity=300, duration=400, limit=2)
    two = b.acquire_image(account, minimum_validity=300, duration=400, limit=2)
    one.release()
    one.release()
    with pytest.raises(CredentialBusy):
        a.begin_refresh(account)
    two.release()
    a.storage.replace_accounts([{**account, "refresh_token": "new-rt"}])
    with pytest.raises(CredentialBusy, match="credential_changed"):
        b.acquire_image(account, minimum_validity=300, duration=400, limit=2)


def test_postgres_result_batch_cas_is_atomic_on_one_conflicted_row(pair):
    a, b, _ = pair
    baseline = [{"access_token": f"batch-{i}", "quota": 8, "success": 0} for i in range(32)]
    a.storage.replace_accounts(baseline)
    b.storage.upsert_account({**baseline[-1], "quota": 4})
    mutation = StorageMutation(upserts=tuple({**r, "quota": 7, "success": 1} for r in baseline))
    with pytest.raises(StorageRevisionConflictError):
        a.storage.mutate_accounts_checked(mutation, expected_items={r["access_token"]: r for r in baseline})
    saved = {r["access_token"]: r for r in b.storage.load_accounts()}
    assert saved["batch-31"]["quota"] == 4
    assert saved["batch-0"]["quota"] == 8
    assert all(r["success"] == 0 for r in saved.values())


def test_postgres_disjoint_result_batches_do_not_conflict_on_collection_revision(pair):
    a, b, _ = pair
    baseline = [{"access_token": f"batch-{i}", "quota": 8, "success": 0} for i in range(64)]
    receipt = a.storage.replace_accounts(baseline)
    barrier = Barrier(2)

    def commit(storage, rows):
        barrier.wait()
        return storage.mutate_accounts_checked(
            StorageMutation(upserts=tuple({**r, "quota": 7, "success": 1} for r in rows),
                            expected_revision=receipt.revision),
            expected_items={r["access_token"]: r for r in rows})

    with ThreadPoolExecutor(2) as pool:
        left = pool.submit(commit, a.storage, baseline[:32])
        right = pool.submit(commit, b.storage, baseline[32:])
        assert left.result(timeout=10).updated == 32
        assert right.result(timeout=10).updated == 32
    assert all(r["quota"] == 7 and r["success"] == 1 for r in a.storage.load_accounts())


def test_postgres_cooldown_and_release_are_one_atomic_handoff(pair, monkeypatch):
    a, b, account = pair
    lease = a.acquire_image(account, minimum_validity=300, duration=400, limit=1, requires_upload=True)
    edited, finish = Event(), Event()
    original = a.edit

    @contextmanager
    def paused_commit(key):
        with original(key) as state:
            yield state
            edited.set()
            assert finish.wait(5)

    monkeypatch.setattr(a, "edit", paused_commit)
    with ThreadPoolExecutor(1) as executor:
        result = executor.submit(lease.release, upload_cooldown_seconds=120)
        try:
            assert edited.wait(2)
            # Even with a second free slot, no request enters the write gap.
            with pytest.raises(CredentialBusy):
                b.acquire_image(account, minimum_validity=300, duration=400, limit=2, requires_upload=True)
        finally:
            finish.set()
        result.result(timeout=5)
    with pytest.raises(CredentialBusy, match="file_upload_throttled") as failure:
        b.acquire_image(account, minimum_validity=300, duration=400, limit=2, requires_upload=True)
    assert 0 < failure.value.retry_after <= 120
    assert "file_upload_blocked_until" not in a.storage.load_accounts()[0]
    text_image = b.acquire_image(account, minimum_validity=300, duration=400, limit=2)
    text_image.release()


def test_postgres_late_shorter_cooldown_and_cleanup_keep_longer_deadline(pair):
    a, b, account = pair
    one = a.acquire_image(account, minimum_validity=300, duration=400, limit=2, requires_upload=True)
    two = b.acquire_image(account, minimum_validity=300, duration=400, limit=2, requires_upload=True)
    one.release(upload_cooldown_seconds=3600)
    with b.edit(one.key) as (_, data, _):
        until = data["file_upload_blocked_until"]
    two.release(upload_cooldown_seconds=60)
    one.release()
    with b.edit(one.key) as (_, data, _):
        assert data["file_upload_blocked_until"] == until
        assert data["leases"] == {}
    restarted = CredentialCoordinator(a.storage)
    with pytest.raises(CredentialBusy, match="file_upload_throttled"):
        restarted.acquire_image(account, minimum_validity=300, duration=400, limit=2, requires_upload=True)


def test_postgres_adaptive_cooldown_survives_coordinator_restart(pair, monkeypatch):
    monkeypatch.setenv("CHATGPT2API_UPLOAD_COOLDOWN_SECONDS", "900")
    a, b, account = pair
    first = a.acquire_image(account, minimum_validity=300, duration=400, limit=2, requires_upload=True)
    state = first.release(upload_cooldown_seconds=900, upload_retry_after_missing=True)
    assert state["file_upload_throttle_streak"] == 1
    with b.edit(first.key) as (_, data, now):
        data["file_upload_blocked_until"] = now - 1
    restarted = CredentialCoordinator(a.storage)
    next_lease = restarted.acquire_image(account, minimum_validity=300, duration=400, limit=2, requires_upload=True)
    state = next_lease.release(upload_cooldown_seconds=900, upload_retry_after_missing=True)
    assert state["file_upload_throttle_streak"] == 2
    with b.edit(first.key) as (_, data, now):
        assert 1790 < data["file_upload_blocked_until"] - now <= 1800

