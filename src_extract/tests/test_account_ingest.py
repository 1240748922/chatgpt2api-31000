"""Durable import checkpoints with real SQLite transactions and isolated rows."""
import json
import time
from threading import Barrier, Event
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.account_ingest_service import AccountIngestService, AccountIngestJob, IngestConflict, IngestLeaseLost
from services.account_service import AccountService
import services.account_service as accounts_module
from services.storage.database_storage import DatabaseStorageBackend
from services.application_database import dispose_database_engine


@pytest.fixture
def ingest(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'ingest.db').as_posix()}"
    db = DatabaseStorageBackend(url)
    monkeypatch.setattr(AccountService, "_load_cumulative_total", lambda self: 0)
    monkeypatch.setattr(AccountService, "_save_cumulative_total", lambda self: None)
    monkeypatch.setattr(accounts_module.log_service, "add", lambda *a, **kw: None)
    accounts = AccountService(db)
    service = AccountIngestService(url, accounts, batch_size=3)
    yield service, db
    accounts._image_result_persist_executor.shutdown(wait=True)
    accounts._image_failure_schedule_executor.shutdown(wait=True)
    dispose_database_engine(url)


def payload(n):
    return [{"access_token": f"secret-test-{i}", "refresh_token": f"private-refresh-{i}"} for i in range(n)]


def expire(service, job_id):
    with service.transaction() as session:
        session.query(AccountIngestJob).filter_by(id=job_id).update({"lease_until": time.time()-1})


def test_save_chunks_resume_after_process_loss_without_replaying_accounts(ingest):
    service, db = ingest
    job = service.submit(payload(7), request_key="same-file")
    assert job["saved"] == 0 and db.load_accounts() == []
    assert service.claim("save", "first") == job["id"]
    view = service.save_batch(job["id"], "first")
    assert view["saved"] == 3 and view["added"] == 3
    assert len(db.load_accounts()) == 3
    assert service.claim("save", "second") is None
    expire(service, job["id"])
    resumed_accounts = AccountService(DatabaseStorageBackend(service.url))
    resumed = AccountIngestService(service.url, resumed_accounts, batch_size=3)
    try:
        assert resumed.claim("save", "second") == job["id"]
        with pytest.raises(IngestLeaseLost):
            service.save_batch(job["id"], "first")
        assert resumed.save_batch(job["id"], "second")["saved"] == 6
        assert resumed.save_batch(job["id"], "second")["status"] == "completed"
        final = resumed.get(job["id"], include_result=True)
        assert final["added"] == final["saved"] == 7
        assert len(final["updated_ids"]) == 7
        assert len(db.load_accounts()) == 7
        with service.Session() as session:
            assert session.get(AccountIngestJob, job["id"]).payload == "[]"
    finally:
        resumed_accounts._image_result_persist_executor.shutdown(wait=True)


def test_account_rows_and_checkpoint_rollback_together(ingest, monkeypatch):
    service, db = ingest
    job = service.submit(payload(3))
    service.claim("save", "worker")
    original = service._finish_save

    def crash(*args):
        original(*args)
        raise RuntimeError("synthetic crash before transaction commit")

    with monkeypatch.context() as patcher:
        patcher.setattr(service, "_finish_save", crash)
        with pytest.raises(RuntimeError):
            service.save_batch(job["id"], "worker")
    assert db.load_accounts() == []
    assert service.get(job["id"])["saved"] == 0
    assert service.save_batch(job["id"], "worker")["added"] == 3


def test_crash_after_commit_keeps_durable_offset(ingest, monkeypatch):
    service, db = ingest
    job = service.submit(payload(6))
    service.claim("save", "worker")
    original = service.accounts.add_account_items

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic crash after commit")

    with monkeypatch.context() as patcher:
        patcher.setattr(service.accounts, "add_account_items", crash)
        with pytest.raises(RuntimeError):
            service.save_batch(job["id"], "worker")
    assert len(db.load_accounts()) == 3
    assert service.get(job["id"])["saved"] == 3
    assert service.save_batch(job["id"], "worker")["added"] == 6


def test_submit_is_idempotent_and_public_views_hide_credentials(ingest):
    service, _ = ingest
    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = list(executor.map(lambda _: service.submit(payload(5), request_key="dedupe"), range(2)))
    assert jobs[0]["id"] == jobs[1]["id"]
    assert len(service.list_jobs()) == 1
    with pytest.raises(IngestConflict):
        service.submit(payload(6), request_key="dedupe")
    public = json.dumps(service.get(jobs[0]["id"], include_result=True)) + json.dumps(service.list_jobs())
    assert "secret-test" not in public and "private-refresh" not in public


def test_create_only_duplicate_does_not_reset_quota_or_disabled_state(ingest):
    service, db = ingest
    service.accounts.add_account_items([{"access_token": "secret-test-0", "status": "禁用", "quota": 0}], return_items=False)
    job = service.submit(payload(2) + payload(1))
    service.claim("save", "worker")
    final = service.save_batch(job["id"], "worker")
    assert final["added"] == 1 and final["skipped"] == 2
    assert len(db.load_accounts()) == 2
    assert service.accounts.get_account("secret-test-0", refresh_snapshot=False)["status"] == "禁用"


def test_quota_checking_cannot_block_next_import(ingest, monkeypatch):
    service, _ = ingest
    first = service.submit(payload(2), sync_after_import=True)
    service.claim("save", "save-worker")
    assert service.save_batch(first["id"], "save-worker")["status"] == "sync_pending"
    second = service.submit([{"access_token": "second-job"}])
    assert service.claim("sync", "sync-worker") == first["id"]
    assert service.claim("save", "save-worker") == second["id"]
    assert service.save_batch(second["id"], "save-worker")["status"] == "completed"
    monkeypatch.setattr(service.accounts, "sync_accounts_and_quota", lambda *a, **kw: {"synced": 1, "errors": [{"error": "secret must not escape"}]})
    final = service.sync_batch(first["id"], "sync-worker")
    assert final["synced"] == final["sync_failed"] == 1
    assert final["checked"] == 2 and final["status"] == "completed"
    assert "secret must not escape" not in json.dumps(final)


def test_ingest_api_requires_admin_and_returns_durable_job(ingest, monkeypatch):
    from api import account_ingest as api
    service, _ = ingest
    monkeypatch.setattr(api, "get_account_ingest_service", lambda: service)
    app = FastAPI()
    app.include_router(api.create_router())
    client = TestClient(app)
    response = client.post("/api/account-import-jobs", json={"accounts": payload(1)})
    assert response.status_code in {401, 403}
    monkeypatch.setattr(api, "require_admin", lambda *a: None)
    response = client.post("/api/account-import-jobs", json={"accounts": payload(1)})
    assert response.status_code == 202
    job_id = response.json()["job"]["id"]
    assert client.get(f"/api/account-import-jobs/{job_id}").json()["job"]["saved"] == 0
    assert "secret-test" not in response.text


def test_conflicting_remote_insert_is_not_counted_or_overwritten(ingest, monkeypatch):
    service, db = ingest
    other = DatabaseStorageBackend(service.url)
    original = db.mutate_accounts_checked
    first = True

    def race(*a, **kw):
        nonlocal first
        if first:
            first = False
            other.upsert_account({"access_token": "secret-test-0", "status": "禁用", "quota": 0})
        return original(*a, **kw)

    monkeypatch.setattr(db, "mutate_accounts_checked", race)
    job = service.submit(payload(2))
    service.claim("save", "worker")
    result = service.save_batch(job["id"], "worker")
    assert result["saved"] == 2 and result["added"] == 1
    assert next(row for row in db.load_accounts() if row["access_token"] == "secret-test-0")["status"] == "禁用"


def test_rt_exchange_partial_failure_is_not_saved_as_an_access_token(ingest, monkeypatch):
    from services.account_service import TerminalRefreshTokenError
    service, db = ingest
    def exchange(rt, account, **kwargs):
        if rt == "rt.bad-secret":
            raise TerminalRefreshTokenError(401, "invalid_grant", "secret rt.bad-secret must never escape")
        return {"access_token": "converted-at-secret", "refresh_token": "rotated-rt-secret", "id_token": "id-secret"}
    monkeypatch.setattr(service.accounts, "_request_access_token_refresh", exchange)
    job = service.submit([{"access_token": "rt.good-secret"}, {"auth": {"refreshToken": "rt.bad-secret"}}, {"accessToken": "ordinary-at"}])
    assert job["refresh_total"] == 2
    assert service.claim("save", "save") is None
    assert service.claim("refresh", "rt") == job["id"]
    view = service.save_batch(job["id"], "rt", refresh=True)
    assert view["done"] and view["processed"] == 3
    assert view["saved"] == view["added"] == 2
    assert view["refresh_done"] == 2 and view["refresh_failed"] == 1 and view["skipped"] == 0
    rows = db.load_accounts()
    assert {r["access_token"] for r in rows} == {"ordinary-at", "converted-at-secret"}
    assert next(r for r in rows if r["access_token"] == "converted-at-secret")["refresh_token"] == "rotated-rt-secret"
    logs = service.events(job["id"])
    assert any(r.get("error_code") == "refresh_token_invalid" for r in logs)
    assert "secret" not in json.dumps([view, logs])


def test_rotated_refresh_credentials_survive_save_failure_without_second_exchange(ingest, monkeypatch):
    service, db = ingest
    called = []
    def exchange(rt, *args, **kwargs):
        called.append(rt)
        return {"access_token": "new-at", "refresh_token": "rotated-rt"}
    monkeypatch.setattr(service.accounts, "_request_access_token_refresh", exchange)
    job = service.submit([{"refresh_token": "rt.original"}])
    service.claim("refresh", "worker")
    def crash(*a, **kw):
        raise RuntimeError("save interrupted after OAuth returned")
    with monkeypatch.context() as patcher:
        patcher.setattr(service.accounts, "add_account_items", crash)
        with pytest.raises(RuntimeError):
            service.save_batch(job["id"], "worker", refresh=True)
    assert not db.load_accounts()
    expire(service, job["id"])
    resumed = AccountIngestService(service.url, service.accounts, batch_size=1)
    resumed.claim("refresh", "new-worker")
    assert resumed.save_batch(job["id"], "new-worker", refresh=True)["saved"] == 1
    assert called == ["rt.original"]
    assert db.load_accounts()[0]["refresh_token"] == "rotated-rt"


def test_rt_network_does_not_block_an_at_job_or_account_reads(ingest, monkeypatch):
    service, db = ingest
    started, release = Event(), Event()
    def exchange(rt, *args, **kwargs):
        assert not service.accounts._lock.locked()
        started.set()
        assert release.wait(10)
        return {"access_token": "converted-at", "refresh_token": rt}
    monkeypatch.setattr(service.accounts, "_request_access_token_refresh", exchange)
    slow = service.submit([{"refresh_token": "rt.slow"}])
    service.claim("refresh", "rt")
    with ThreadPoolExecutor(max_workers=2) as pool:
        background = pool.submit(service.save_batch, slow["id"], "rt", refresh=True)
        try:
            assert started.wait(3)
            fast = service.submit([{"access_token": "fast-at"}])
            assert service.claim("save", "at") == fast["id"]
            assert pool.submit(service.save_batch, fast["id"], "at").result(timeout=3)["saved"] == 1
            assert service.accounts.get_account("fast-at", refresh_snapshot=False)
        finally:
            release.set()
        assert background.result(timeout=3)["saved"] == 1


def test_rt_exchange_uses_bounded_parallelism(ingest, monkeypatch):
    import services.account_ingest_service as module
    service, _ = ingest
    barrier = Barrier(2)
    monkeypatch.setattr(module, "account_import_worker_count", lambda n: min(2, n))
    def exchange(rt, *a, **kw):
        barrier.wait(timeout=3)  # cannot pass if refresh is accidentally serial
        return {"access_token": "at-" + rt, "refresh_token": rt}
    monkeypatch.setattr(service.accounts, "_request_access_token_refresh", exchange)
    service.batch_size = 4
    job = service.submit([{"refresh_token": f"rt.parallel-{i}"} for i in range(4)])
    service.claim("refresh", "rt")
    view = service.save_batch(job["id"], "rt", refresh=True)
    assert view["saved"] == 4 and view["refresh_failed"] == 0


def test_new_batch_size_resumes_existing_chunks_and_old_payloads(ingest):
    from services.account_ingest_service import AccountIngestChunk
    service, db = ingest
    job = service.submit(payload(7))
    service.claim("save", "worker")
    assert service.save_batch(job["id"], "worker")["saved"] == 3
    service.batch_size = 2
    assert service.save_batch(job["id"], "worker")["saved"] == 5
    assert service.save_batch(job["id"], "worker")["saved"] == 6
    assert service.save_batch(job["id"], "worker")["saved"] == 7
    # An already-queued job from the previous release retains its old format.
    old = service.submit([{"access_token": "legacy-at"}])
    with service.transaction() as session:
        session.query(AccountIngestChunk).filter_by(job_id=old["id"]).delete()
        session.get(AccountIngestJob, old["id"]).payload = json.dumps([{"access_token": "legacy-at"}])
    service.claim("save", "worker")
    assert service.save_batch(old["id"], "worker")["saved"] == 1
    assert len(db.load_accounts()) == 8


def test_rt_quota_phase_checks_converted_at_and_clears_payloads(ingest, monkeypatch):
    from services.account_ingest_service import AccountIngestChunk, AccountIngestRefresh
    service, _ = ingest
    monkeypatch.setattr(service.accounts, "_request_access_token_refresh", lambda *a, **kw: {"access_token": "converted-at", "refresh_token": "rotated-rt"})
    seen = []
    def sync(tokens, **kwargs):
        seen.extend(tokens)
        return {"synced": len(tokens), "errors": []}
    monkeypatch.setattr(service.accounts, "sync_accounts_and_quota", sync)
    job = service.submit([{"refresh_token": "rt.original"}], sync_after_import=True)
    service.claim("refresh", "rt")
    assert service.save_batch(job["id"], "rt", refresh=True)["status"] == "sync_pending"
    service.claim("sync", "sync")
    assert service.sync_batch(job["id"], "sync")["status"] == "completed"
    assert seen == ["converted-at"]
    with service.Session() as session:
        assert session.query(AccountIngestChunk).filter_by(job_id=job["id"]).one().payload == "[]"
        assert session.query(AccountIngestRefresh).filter_by(job_id=job["id"]).one().payload == "{}"


def test_event_cursor_and_api_refresh_tokens(ingest, monkeypatch):
    from api import account_ingest as api
    service, _ = ingest
    monkeypatch.setattr(api, "get_account_ingest_service", lambda: service)
    app = FastAPI()
    app.include_router(api.create_router())
    client = TestClient(app)
    assert client.get("/api/account-import-jobs/anything/events").status_code in {401, 403}
    monkeypatch.setattr(api, "require_admin", lambda *a: None)
    response = client.post("/api/account-import-jobs", json={"tokens": ["rt.one"], "refresh_tokens": ["rt.two"]})
    assert response.status_code == 202 and response.json()["job"]["refresh_total"] == 2
    job_id = response.json()["job"]["id"]
    service.claim("refresh", "worker")
    first = client.get(f"/api/account-import-jobs/{job_id}/events?limit=1").json()
    second = client.get(f"/api/account-import-jobs/{job_id}/events?after={first['next_cursor']}").json()
    assert first["events"][0]["code"] == "submitted"
    assert second["events"][0]["code"] == "phase_started"
    assert "rt.one" not in json.dumps([first, second])


def test_quota_none_result_counts_as_failed_check(ingest, monkeypatch):
    service, _ = ingest
    monkeypatch.setattr(service.accounts, "sync_accounts_and_quota", lambda *a, **kw: {"synced": 0, "errors": []})
    job = service.submit(payload(1), sync_after_import=True)
    service.claim("save", "save")
    service.save_batch(job["id"], "save")
    service.claim("sync", "sync")
    result = service.sync_batch(job["id"], "sync")
    assert result["checked"] == result["sync_failed"] == 1
