"""Slow pool refreshes must not delay durable foreground token rotation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace

import pytest

from test_account_write_contention import account_flow
from test_database_snapshot_concurrency import databases
from test_account_dispatch_isolation import pool


@pytest.mark.parametrize("stalled_read", ["get_collection_revision", "load_accounts_snapshot"])
def test_slow_snapshot_does_not_block_credential_save_or_generation(account_flow, monkeypatch, stalled_read):
    f = account_flow
    f.writer.upsert_account({"access_token": "remote-import", "quota": 8, "status": "正常"})
    f.service._account_snapshot_checked_at = float("-inf")
    monkeypatch.setattr(f.service, "_schedule_accounts_snapshot_refresh", lambda **kw: None)
    entered, release, completed = Event(), Event(), Event()
    original = getattr(f.reader, stalled_read)
    first = True
    def slow(*args, **kwargs):
        nonlocal first
        result = original(*args, **kwargs)
        if first:
            first = False
            entered.set()
            assert release.wait(10)
        return result
    monkeypatch.setattr(f.reader, stalled_read, slow)
    with ThreadPoolExecutor(max_workers=2) as executor:
        refresh = executor.submit(f.service._refresh_accounts_snapshot_if_stale, wait_for_refresh=True)
        try:
            assert entered.wait(5)
            request = executor.submit(f.run)
            request.add_done_callback(lambda _: completed.set())
            assert completed.wait(2), "foreground token save waited for the slow pool snapshot"
            assert request.result(timeout=1)[0].kind == "result"
            assert not refresh.done(), "snapshot barrier was not held during generation"
        finally:
            release.set()
        assert refresh.result(timeout=5)
        request.result(timeout=5)
    assert f.selected == f.closed == ["new-test-token"]
    assert f.exchanges == [True]
    assert f.service._image_inflight == {}
    rows = {row["access_token"]: row for row in f.writer.load_accounts()}
    assert "old-test-token" not in rows and "old-test-token" not in f.service._accounts
    assert f.service._accounts["new-test-token"]["refresh_token"] == "new-test-refresh"
    assert rows["new-test-token"]["refresh_token"] == "new-test-refresh"
    assert "remote-import" in f.service._accounts
    assert f.service._snapshot_write_journal is None


@contextmanager
def paused_snapshot(pool, monkeypatch, *, before_read=False):
    service, db, remote = pool
    remote.upsert_account({"access_token": "remote-import", "quota": 6, "status": "正常"})
    service._account_snapshot_checked_at = float("-inf")
    monkeypatch.setattr(service, "_schedule_accounts_snapshot_refresh", lambda **kw: None)
    original = db.load_accounts_snapshot
    entered, release = Event(), Event()
    captured = []
    first = True
    def read():
        nonlocal first
        if not first:
            return original()
        first = False
        if not before_read:
            captured.append(original())
        entered.set()
        assert release.wait(10)
        if before_read:
            captured.append(original())
        return captured[0]
    monkeypatch.setattr(db, "load_accounts_snapshot", read)
    with ThreadPoolExecutor(max_workers=2) as executor:
        refresh = executor.submit(service._refresh_accounts_snapshot_if_stale, wait_for_refresh=True)
        try:
            assert entered.wait(5)
            yield SimpleNamespace(service=service, db=db, remote=remote, captured=captured,
                                  refresh=refresh, release=release, executor=executor)
        finally:
            release.set()
            refresh.result(timeout=5)
    assert service._snapshot_write_journal is None


@pytest.mark.parametrize("operation", ["quota", "import", "delete", "credentials"])
def test_old_snapshot_does_not_roll_back_concurrent_committed_writes(pool, monkeypatch, operation):
    with paused_snapshot(pool, monkeypatch) as p:
        operations = {
            "quota": lambda: p.service.mark_image_result("healthy", True),
            "import": lambda: p.service.add_accounts(["local-import"], return_items=False),
            "delete": lambda: p.service.delete_accounts(["healthy"], return_items=False),
            "credentials": lambda: p.service.update_account("healthy", {"refresh_token": "new-refresh"}, quiet=True),
        }
        p.executor.submit(operations[operation]).result(timeout=2)
        assert not p.refresh.done()
    rows = {row["access_token"]: row for row in p.remote.load_accounts()}
    assert p.refresh.result() is True
    assert "remote-import" in p.service._accounts
    assert set(p.service._accounts) == set(rows)
    for key in rows:
        assert p.service._accounts[key]["quota"] == rows[key]["quota"]
        assert p.service._accounts[key].get("refresh_token") == rows[key].get("refresh_token")
    if operation == "quota":
        assert p.service._accounts["healthy"]["success"] == rows["healthy"]["success"] == 1
        assert p.service._accounts["healthy"]["quota"] == 9
    assert p.service._accounts_revision == p.captured[0].revision
    assert p.service._accounts_revision != p.remote.get_collection_revision("accounts")


@pytest.mark.parametrize("newer_change", ["delete", "credentials"])
def test_newer_remote_snapshot_wins_over_earlier_local_commit(pool, monkeypatch, newer_change):
    with paused_snapshot(pool, monkeypatch, before_read=True) as p:
        p.executor.submit(p.service.update_account, "healthy", {"notes": "local"}, quiet=True).result(timeout=2)
        if newer_change == "delete":
            p.remote.delete_account("healthy")
        else:
            row = next(row for row in p.remote.load_accounts() if row["access_token"] == "healthy")
            p.remote.upsert_account({**row, "refresh_token": "remote-newest", "last_token_refresh_at": "later"})
    assert p.refresh.result() is True
    if newer_change == "delete":
        assert "healthy" not in p.service._accounts
    else:
        assert p.service._accounts["healthy"]["refresh_token"] == "remote-newest"
    assert p.service._accounts_revision == p.remote.get_collection_revision("accounts")


def test_unflushed_completion_during_refresh_is_preserved_once(pool, monkeypatch):
    service, db, _ = pool
    monkeypatch.setattr(service, "_schedule_image_result_persist", lambda *a: None)
    with paused_snapshot(pool, monkeypatch) as p:
        p.executor.submit(service.mark_image_result, "healthy", True, defer_persistence=True).result(timeout=2)
    assert service._accounts["healthy"]["success"] == 1
    assert service._accounts["healthy"]["quota"] == 9
    assert service._persisted_accounts["healthy"]["quota"] == 10
    with service._account_write():
        service._save_accounts(account_tokens={"healthy"})
    row = next(row for row in db.load_accounts() if row["access_token"] == "healthy")
    assert row["success"] == 1 and row["quota"] == 9


def test_unknown_receipt_discards_snapshot_without_losing_saved_credentials(pool, monkeypatch):
    service, db, _ = pool
    original = db.mutate_accounts_checked
    def no_receipt(*args, **kwargs):
        original(*args, **kwargs)
        return None
    monkeypatch.setattr(db, "mutate_accounts_checked", no_receipt)
    revision = service._accounts_revision
    with paused_snapshot(pool, monkeypatch) as p:
        p.executor.submit(service.update_account, "healthy", {"refresh_token": "new-refresh"}, quiet=True).result(timeout=2)
    assert p.refresh.result() is False
    assert service._accounts["healthy"]["refresh_token"] == "new-refresh"
    assert service._accounts_revision == revision
    # Retry has no overlapping commit and can publish normally.
    assert service._refresh_accounts_snapshot_if_stale(wait_for_refresh=True)
    assert "remote-import" in service._accounts
    assert service._accounts["healthy"]["refresh_token"] == "new-refresh"


def test_write_started_before_snapshot_is_reconciled_at_publication(pool, monkeypatch):
    service, db, remote = pool
    remote.upsert_account({"access_token": "remote-import", "quota": 6})
    service._account_snapshot_checked_at = float("-inf")
    started, release, read_done = Event(), Event(), Event()
    original_save, original_read = db.mutate_accounts_checked, db.load_accounts_snapshot
    def save(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original_save(*args, **kwargs)
    def read():
        result = original_read()
        read_done.set()
        return result
    monkeypatch.setattr(db, "mutate_accounts_checked", save)
    monkeypatch.setattr(db, "load_accounts_snapshot", read)
    with ThreadPoolExecutor(max_workers=2) as executor:
        write = executor.submit(service.update_account, "healthy", {"notes": "committed"}, quiet=True)
        try:
            assert started.wait(5)
            refresh = executor.submit(service._refresh_accounts_snapshot_if_stale, wait_for_refresh=True)
            assert read_done.wait(2), "snapshot read waited for writer I/O"
            assert not refresh.done(), "uncommitted rows were published"
        finally:
            release.set()
        write.result(timeout=5)
        assert refresh.result(timeout=5)
    assert service._accounts["healthy"]["notes"] == "committed"
    assert "remote-import" in service._accounts
    assert service._snapshot_write_journal is None


def test_write_receipt_does_not_cover_unchanged_rows_in_the_requested_batch(pool, monkeypatch):
    service, _db, remote = pool
    other = next(row for row in remote.load_accounts() if row["access_token"] == "other")
    remote.upsert_account({**other, "quota": 4})
    def write():
        with service._account_write():
            service._accounts["healthy"] = {**service._accounts["healthy"], "notes": "changed"}
            # Only healthy is actually written. 'other' still has an old local
            # baseline and must not inherit healthy's newer commit receipt.
            service._save_accounts(account_tokens={"healthy", "other"})
    with paused_snapshot(pool, monkeypatch) as p:
        p.executor.submit(write).result(timeout=2)
    assert service._accounts["healthy"]["notes"] == "changed"
    assert service._accounts["other"]["quota"] == 4
