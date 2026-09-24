"""Result persistence must not turn a burst into full-pool, serial writes."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event

import pytest

from services.storage.base import StorageMutation
from test_account_auth_quarantine import service_factory, row


def queue_results(service, monkeypatch, tokens):
    # Deterministic burst: queue through the real completion path, then run its
    # single writer ourselves. No network or real credentials are used.
    monkeypatch.setattr(service._image_result_persist_executor, "submit", lambda fn: None)
    for token in tokens:
        service.mark_image_result(token, True, defer_persistence=True)


def test_burst_uses_bounded_batches_and_preserves_every_result(service_factory, monkeypatch):
    tokens = [f"synthetic-{i}" for i in range(100)]
    service = service_factory([row(token) for token in tokens])
    queue_results(service, monkeypatch, tokens)
    writes = []
    original = service.storage.mutate_accounts_checked

    def save(mutation, **kw):
        writes.append(len(mutation.upserts))
        return original(mutation, **kw)

    monkeypatch.setattr(service.storage, "mutate_accounts_checked", save)
    service._flush_image_result_persistence()
    assert len(writes) <= 4, writes
    assert max(writes) <= 32
    assert sum(writes) == 100
    assert all(r["quota"] == 7 and r["success"] == 1 for r in service.storage.load_accounts())
    assert not service._image_result_persist_pending
    assert not service._image_result_persist_scheduled


def test_result_conflict_reads_only_affected_rows_in_large_pool(service_factory, monkeypatch):
    service = service_factory([row("used"), row("other")])
    service.storage.mutate_accounts(StorageMutation(upserts=tuple(
        row(f"unrelated-{i}") for i in range(20000))))
    queue_results(service, monkeypatch, ["used"])
    remote = deepcopy(service._persisted_accounts["used"])
    remote.update(quota=6, success=2, notes="remote change")
    service.storage.upsert_account(remote)
    full_reads, scoped_reads = [], []
    original_full = service.storage.load_accounts_snapshot
    original_subset = service.storage.load_accounts_subset_snapshot

    def full():
        full_reads.append(True)
        return original_full()

    def subset(keys):
        scoped_reads.append(set(keys))
        return original_subset(keys)

    monkeypatch.setattr(service.storage, "load_accounts_snapshot", full)
    monkeypatch.setattr(service.storage, "load_accounts_subset_snapshot", subset)
    service._flush_image_result_persistence()
    assert full_reads == [], "ordinary result conflict reloaded all 20,002 accounts under writer lock"
    assert scoped_reads == [{"used"}]
    saved = original_subset(["used"]).items[0]
    assert saved["quota"] == 5 and saved["success"] == 3
    assert saved["notes"] == "remote change"
    # A row receipt is not a full collection snapshot.
    assert "unrelated-0" not in service._accounts
    service._account_snapshot_checked_at = 0
    assert service._refresh_accounts_snapshot_if_stale()
    assert "unrelated-0" in service._accounts


@pytest.mark.parametrize("change", ["delete", "rotate"])
def test_batch_keeps_remote_deletion_and_rotation_authoritative(service_factory, monkeypatch, change):
    service = service_factory([row("used"), row("other")])
    queue_results(service, monkeypatch, ["used", "other"])
    remote = deepcopy(service._persisted_accounts["used"])
    if change == "delete":
        service.storage.delete_account("used")
    else:
        remote.update(access_token="rotated", refresh_token="new-synthetic-rt", last_token_refresh_at="new")
        service.storage.mutate_accounts(StorageMutation(upserts=(remote,), delete_keys=("used",)))
    service._flush_image_result_persistence()
    rows = {r["access_token"]: r for r in service.storage.load_accounts()}
    assert "used" not in rows
    assert rows["other"]["success"] == 1 and rows["other"]["quota"] == 7
    if change == "rotate":
        assert rows["rotated"]["refresh_token"] == "new-synthetic-rt"
        assert rows["rotated"]["success"] == 1 and rows["rotated"]["quota"] == 7
        assert service.resolve_access_token("used") == "rotated"


def test_completion_during_batch_commits_latest_delta_once(service_factory, monkeypatch):
    service = service_factory([row("used"), row("other")])
    queue_results(service, monkeypatch, ["used", "other"])
    entered, release = Event(), Event()
    original = service.storage.mutate_accounts_checked
    first = True

    def save(*args, **kwargs):
        nonlocal first
        assert not service._lock.locked()
        if first:
            first = False
            entered.set()
            assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(service.storage, "mutate_accounts_checked", save)
    with ThreadPoolExecutor(max_workers=1) as pool:
        flush = pool.submit(service._flush_image_result_persistence)
        try:
            assert entered.wait(5)
            service.mark_image_result("used", True, defer_persistence=True)
            assert service.get_account("other", refresh_snapshot=False)
        finally:
            release.set()
        flush.result(timeout=5)
    rows = {r["access_token"]: r for r in service.storage.load_accounts()}
    assert rows["used"]["success"] == 2 and rows["used"]["quota"] == 6
    assert rows["other"]["success"] == 1 and rows["other"]["quota"] == 7


def test_deleted_queued_row_is_not_recreated_by_stale_payload(service_factory, monkeypatch):
    service = service_factory([row("used"), row("other")])
    queue_results(service, monkeypatch, ["used", "other"])
    service.storage.delete_account("used")
    with service._account_write():
        service._accounts.pop("used")
        service._persisted_accounts.pop("used")
    service._flush_image_result_persistence()
    assert {r["access_token"] for r in service.storage.load_accounts()} == {"other"}


def test_scoped_conflict_does_not_commit_unrelated_pending_result(service_factory, monkeypatch):
    service = service_factory([row("used"), row("pending")])
    queue_results(service, monkeypatch, ["pending"])
    remote = deepcopy(service._persisted_accounts["used"])
    service.storage.upsert_account({**remote, "notes": "remote"})
    service.update_account("used", {"email": "synthetic@example.test"}, quiet=True)
    saved = {r["access_token"]: r for r in service.storage.load_accounts()}
    assert saved["used"]["notes"] == "remote"
    assert saved["pending"]["success"] == 0 and saved["pending"]["quota"] == 8
    assert service._accounts["pending"]["success"] == 1
    assert service._accounts["pending"]["quota"] == 7
    service._flush_image_result_persistence()
    pending = service.storage.load_accounts_subset_snapshot(["pending"]).items[0]
    assert pending["success"] == 1 and pending["quota"] == 7


def test_batch_deduplicates_queued_alias_after_local_rotation(service_factory, monkeypatch):
    service = service_factory([row("used")])
    queue_results(service, monkeypatch, ["used"])
    assert service.update_account("used", {"access_token": "rotated"}, quiet=True)
    service.mark_image_result("rotated", True, defer_persistence=True)
    service._flush_image_result_persistence()
    saved = service.storage.load_accounts()
    assert len(saved) == 1
    assert saved[0]["access_token"] == "rotated"
    assert saved[0]["success"] == 2 and saved[0]["quota"] == 6
