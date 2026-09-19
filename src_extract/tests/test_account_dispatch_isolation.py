"""Database stalls must not stop healthy leases or image completions."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event

import pytest

import services.account_service as module
from services.account_service import AccountService
from test_database_snapshot_concurrency import databases


@pytest.fixture
def pool(databases, monkeypatch):
    db, remote = databases
    db.replace_accounts([{"access_token": key, "status": "正常", "quota": 10,
                          "last_remote_checked_at": "2026-09-01T00:00:00+00:00",
                          "image_quota_unknown": False} for key in ("healthy", "other")])
    monkeypatch.setattr(AccountService, "_load_cumulative_total", lambda self: 0)
    monkeypatch.setattr(AccountService, "_save_cumulative_total", lambda self: None)
    monkeypatch.setattr(module.log_service, "add", lambda *a, **kw: None)
    service = AccountService(db)
    service._ACCOUNT_SNAPSHOT_TTL_SECONDS = 3600
    service._image_shard_count = 1
    yield service, db, remote
    service._image_result_persist_executor.shutdown(wait=True)


@pytest.mark.parametrize("operation", ["quota", "import", "failure"])
def test_slow_database_does_not_hold_dispatch_lock(pool, monkeypatch, operation):
    service, db, _ = pool
    entered, release, progressed = Event(), Event(), Event()
    original = db.mutate_accounts_checked

    def save(*a, **kw):
        assert not service._lock.locked()
        entered.set()
        assert release.wait(10)
        if operation == "failure":
            raise RuntimeError("synthetic DB failure")
        return original(*a, **kw)

    monkeypatch.setattr(db, "mutate_accounts_checked", save)

    def foreground():
        assert service.get_account("healthy", refresh_snapshot=False)
        assert service.get_account("new", refresh_snapshot=False) is None, "uncommitted rows must stay private"
        token = service.get_available_access_token()
        service.mark_image_result(token, True, defer_persistence=True)
        progressed.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(service.add_accounts, ["new"], return_items=False) if operation != "quota" else executor.submit(service.update_account, "other", {"quota": 7}, quiet=True)
        try:
            assert entered.wait(5)
            request = executor.submit(foreground)
            assert progressed.wait(2), "healthy request waited for unrelated DB write"
        finally:
            release.set()
        if operation == "failure":
            with pytest.raises(RuntimeError):
                writer.result(timeout=5)
        else:
            writer.result(timeout=5)
        request.result(timeout=5)


def test_account_group_counts_does_not_materialize_full_account_records(pool, monkeypatch):
    service, _db, _remote = pool
    service._accounts["healthy"]["group_id"] = "primary"
    service._accounts["other"]["group_id"] = "primary"
    service._accounts["third"] = {"access_token": "third", "group_id": "secondary"}
    monkeypatch.setattr(service, "list_accounts", lambda: pytest.fail("must not copy account records"))

    assert service.account_group_counts() == {"primary": 2, "secondary": 1}


def test_completion_during_same_account_update_is_merged_once(pool, monkeypatch):
    service, db, _ = pool
    entered, release = Event(), Event()
    original = db.mutate_accounts_checked
    first = True

    def save(*a, **kw):
        nonlocal first
        if first:
            first = False
            entered.set()
            assert release.wait(10)
        return original(*a, **kw)

    monkeypatch.setattr(db, "mutate_accounts_checked", save)
    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(service.update_account, "healthy", {"notes": "new note"}, quiet=True)
        try:
            assert entered.wait(5)
            service.mark_image_result("healthy", True, defer_persistence=True)
            service.mark_image_result("healthy", True, defer_persistence=True)
        finally:
            release.set()
        writer.result(timeout=5)
    service._image_result_persist_executor.shutdown(wait=True)
    row = next(row for row in db.load_accounts() if row["access_token"] == "healthy")
    assert row["notes"] == "new note"
    assert row["success"] == 2
    assert row["quota"] == 8


def test_background_result_does_not_resurrect_remote_deletion(pool):
    service, db, remote = pool
    with service._write_lock:
        service.mark_image_result("healthy", True, defer_persistence=True)
        remote.delete_account("healthy")
    service._image_result_persist_executor.shutdown(wait=True)
    assert "healthy" not in {row["access_token"] for row in db.load_accounts()}
    assert service.get_account("healthy", refresh_snapshot=False) is None


def test_import_ignores_unrelated_revision_changes(pool, monkeypatch):
    service, db, remote = pool
    original = db.mutate_accounts_checked
    calls = []

    def save(*a, **kw):
        calls.append(True)
        remote.upsert_account({"access_token": "unrelated", "quota": len(calls)})
        return original(*a, **kw)

    monkeypatch.setattr(db, "mutate_accounts_checked", save)
    result = service.add_accounts([f"new-{i}" for i in range(500)], return_items=False)
    assert result["added"] == 500
    assert len(calls) == 1
    assert len(db.load_accounts()) == 503


def test_create_only_import_keeps_existing_state_during_conflict(pool, monkeypatch):
    service, db, remote = pool
    original = db.mutate_accounts_checked
    existing = {"access_token": "new", "status": "禁用", "quota": 0}
    first = True

    def save(*a, **kw):
        nonlocal first
        if first:
            first = False
            remote.upsert_account(existing)
        return original(*a, **kw)

    monkeypatch.setattr(db, "mutate_accounts_checked", save)
    result = service.add_account_items([{"access_token": "new", "status": "正常", "quota": 10}],
                                       return_items=False, skip_existing=True)
    assert result["added"] == 0
    assert service.get_account("new", refresh_snapshot=False)["status"] == "禁用"
    row = next(row for row in db.load_accounts() if row["access_token"] == "new")
    assert row["status"] == "禁用" and row["quota"] == 0
