"""Durable, bounded account ingestion; no image workers are used.

The save checkpoint commits in the SAME transaction as account rows. A crash
cannot replay an acknowledged batch or report rows saved before they exist.
Quota checking has a separate worker and can never hold up the next import.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import logging
import threading
import time
import uuid

from sqlalchemy import Column, Float, Integer, String, Text, or_, and_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from services.application_database import DatabaseBase, initialize_application_database, resolve_database_url
from services.storage.database_storage import account_commit_hook
from services.runtime_configuration import env_int

logger = logging.getLogger(__name__)


class AccountIngestJob(DatabaseBase):
    __tablename__ = "account_ingest_jobs"
    id = Column(String(40), primary_key=True)
    request_key = Column(String(64), unique=True, nullable=False)
    fingerprint = Column(String(64), nullable=False)
    status = Column(String(24), nullable=False, index=True)
    created = Column(Float, nullable=False, index=True)
    updated = Column(Float, nullable=False)
    owner = Column(String(64), nullable=False, default="")
    lease_until = Column(Float, nullable=False, default=0)
    retry_at = Column(Float, nullable=False, default=0)
    failures = Column(Integer, nullable=False, default=0)
    total = Column(Integer, nullable=False)
    input_duplicates = Column(Integer, nullable=False, default=0)
    saved = Column(Integer, nullable=False, default=0)
    added = Column(Integer, nullable=False, default=0)
    checked = Column(Integer, nullable=False, default=0)
    synced = Column(Integer, nullable=False, default=0)
    sync_failed = Column(Integer, nullable=False, default=0)
    sync_after = Column(Integer, nullable=False, default=0)
    payload = Column(Text, nullable=False)  # credentials: never included in public views
    updated_ids = Column(Text, nullable=False, default="[]")
    error = Column(String(80), nullable=False, default="")


class IngestConflict(ValueError):
    pass


class IngestLeaseLost(RuntimeError):
    pass


class AccountIngestService:
    LEASE_SECONDS = 60

    def __init__(self, database_url=None, account_service=None, *, batch_size=None):
        self.url = database_url or resolve_database_url()
        self.engine = initialize_application_database(self.url)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._accounts = account_service
        self.batch_size = batch_size or env_int("CHATGPT2API_IMPORT_BATCH_SIZE", 250, 1, 1000)
        self._stop = threading.Event()
        self._threads = []
        self._start_lock = threading.Lock()

    @property
    def accounts(self):
        if self._accounts is None:
            from services.account_service import account_service
            self._accounts = account_service
        return self._accounts

    @contextmanager
    def transaction(self):
        with self.Session() as session:
            try:
                if self.engine.dialect.name == "sqlite":
                    session.execute(text("BEGIN IMMEDIATE"))
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    @staticmethod
    def public(row, *, include_result=False):
        result = {
            "id": row.id, "status": row.status, "total": row.total,
            "saved": row.saved, "added": row.added,
            "skipped": max(0, row.saved - row.added) + row.input_duplicates,
            "checked": row.checked, "synced": row.synced, "sync_failed": row.sync_failed,
            "sync_after_import": bool(row.sync_after), "error_code": row.error,
            "created_at": row.created, "updated_at": row.updated,
            "done": row.status in {"completed", "failed"},
        }
        if include_result and result["done"]:
            result["updated_ids"] = json.loads(row.updated_ids)
        return result

    def submit(self, items, *, sync_after_import=False, request_key=None):
        if not items or len(items) > 50000:
            raise ValueError("每个任务需要 1～50000 个账号")
        # Deduplicate once, before taking any account-pool lock. Do not reset
        # existing lifecycle/quota state on re-import: this is a replenish path.
        unique = {}
        for item in items:
            prepared = self.accounts._prepare_account_payload(item)
            if prepared is None:
                raise ValueError("账号缺少 access_token")
            unique[prepared["access_token"]] = prepared
        payload = json.dumps(list(unique.values()), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if len(payload.encode()) > 64 * 1024 * 1024:
            raise ValueError("导入内容超过 64 MiB，请拆分文件")
        fingerprint = hashlib.sha256((str(bool(sync_after_import)) + payload).encode()).hexdigest()
        key = hashlib.sha256(str(request_key or uuid.uuid4().hex).encode()).hexdigest()
        now = time.time()
        try:
            with self.transaction() as session:
                existing = session.query(AccountIngestJob).filter_by(request_key=key).first()
                if existing:
                    if existing.fingerprint != fingerprint:
                        raise IngestConflict("同一幂等键不能提交不同内容")
                    return self.public(existing)
                if session.query(AccountIngestJob).filter(AccountIngestJob.status.notin_(["completed", "failed"])).count() >= 100:
                    raise IngestConflict("导入队列已满，请稍后再提交")
                row = AccountIngestJob(
                    id=uuid.uuid4().hex, request_key=key, fingerprint=fingerprint,
                    status="queued", created=now, updated=now, total=len(unique),
                    input_duplicates=len(items)-len(unique), sync_after=int(bool(sync_after_import)),
                    payload=payload, updated_ids="[]", owner="", lease_until=0, retry_at=0,
                    saved=0, added=0, checked=0, synced=0, sync_failed=0, failures=0, error="",
                )
                session.add(row)
                session.flush()
                return self.public(row)
        except IntegrityError:
            # Concurrent identical POSTs race on a unique key, never create two jobs.
            with self.Session() as session:
                row = session.query(AccountIngestJob).filter_by(request_key=key).one()
                if row.fingerprint != fingerprint:
                    raise IngestConflict("同一幂等键不能提交不同内容")
                return self.public(row)

    def get(self, job_id, *, include_result=False):
        with self.Session() as session:
            row = session.get(AccountIngestJob, job_id)
            return self.public(row, include_result=include_result) if row else None

    def list_jobs(self, limit=30):
        with self.Session() as session:
            rows = session.query(AccountIngestJob).order_by(AccountIngestJob.created.desc()).limit(min(100, max(1, limit))).all()
            return [self.public(row) for row in rows]

    def retry(self, job_id):
        with self.transaction() as session:
            row = session.query(AccountIngestJob).filter_by(id=job_id).with_for_update().one_or_none()
            if row is None:
                return None
            if row.status != "failed":
                raise IngestConflict("只有失败任务可以重试")
            row.status = "sync_pending" if row.saved == row.total else "queued"
            row.owner, row.lease_until, row.retry_at, row.failures, row.error = "", 0, 0, 0, ""
            row.updated = time.time()
            return self.public(row)

    def claim(self, phase, owner):
        pending, running = ("queued", "saving") if phase == "save" else ("sync_pending", "syncing")
        now = time.time()
        with self.transaction() as session:
            eligible = and_(AccountIngestJob.retry_at <= now, or_(
                AccountIngestJob.status == pending,
                and_(AccountIngestJob.status == running, AccountIngestJob.lease_until < now),
            ))
            row = session.query(AccountIngestJob).filter(eligible).order_by(AccountIngestJob.created).with_for_update(skip_locked=True).first()
            if row is None:
                return None
            row.owner, row.lease_until, row.status, row.updated = owner, now+self.LEASE_SECONDS, running, now
            return row.id

    def _owned(self, session, job_id, owner, status, offset=None):
        row = session.query(AccountIngestJob).filter_by(id=job_id).with_for_update().one()
        if row.owner != owner or row.status != status or row.lease_until < time.time():
            raise IngestLeaseLost("import lease lost")
        if offset is not None and row.saved != offset:
            raise IngestLeaseLost("import checkpoint already advanced")
        return row

    def heartbeat(self, job_id, owner):
        with self.transaction() as session:
            return session.query(AccountIngestJob).filter_by(id=job_id, owner=owner).filter(
                AccountIngestJob.status.in_(["saving", "syncing"]),
                AccountIngestJob.lease_until >= time.time(),
            ).update({"lease_until": time.time()+self.LEASE_SECONDS}, synchronize_session=False)

    @staticmethod
    def _finish_save(row, end, new_ids):
        previous_ids = json.loads(row.updated_ids)
        ids = list(dict.fromkeys([*previous_ids, *new_ids]))
        new_unique = [account_id for account_id in ids if account_id not in previous_ids]
        row.saved = end
        row.updated_ids = json.dumps(ids)
        # Keep the counter tied to this checkpoint's actual inserts. This is
        # equivalent to len(ids) for a clean job, but remains correct if a
        # recovered row already contains a partial result list.
        row.added += len(new_unique)
        row.updated = time.time()
        row.failures, row.error = 0, ""
        if end == row.total:
            row.status = "sync_pending" if row.sync_after else "completed"
            row.owner, row.lease_until = "", 0
            if not row.sync_after:
                row.payload = "[]"  # minimize duplicate credential retention

    def save_batch(self, job_id, owner):
        with self.Session() as session:
            row = session.get(AccountIngestJob, job_id)
            if row is None:
                raise IngestLeaseLost("import job missing")
            start, end = row.saved, min(row.total, row.saved+self.batch_size)
            items = json.loads(row.payload)[start:end]
        committed = False

        def checkpoint(session, stage, mutation, counts):
            nonlocal committed
            row = self._owned(session, job_id, owner, "saving", start)
            if stage == "after":
                # Create-only batches never alter existing account rows. IDs
                # are committed with the rows, not reconstructed after a crash.
                inserted_keys = set(counts["inserted_keys"])
                ids = [item["management_id"] for item in mutation.upserts if item["access_token"] in inserted_keys]
                self._finish_save(row, end, ids)
                committed = True

        with account_commit_hook(checkpoint, self.accounts.storage):
            self.accounts.add_account_items(items, return_items=False, skip_existing=True)
        # A batch containing only existing/rotated tokens requires no account
        # write. Advancing its checkpoint alone is then safe and idempotent.
        if not committed:
            with self.transaction() as session:
                row = self._owned(session, job_id, owner, "saving", start)
                self._finish_save(row, end, [])
        return self.get(job_id)

    def sync_batch(self, job_id, owner):
        with self.Session() as session:
            row = session.get(AccountIngestJob, job_id)
            start, end = row.checked, min(row.total, row.checked+10)
            tokens = [item["access_token"] for item in json.loads(row.payload)[start:end]]
        result = self.accounts.sync_accounts_and_quota(tokens, include_items=False)
        with self.transaction() as session:
            row = self._owned(session, job_id, owner, "syncing")
            if row.checked != start:
                raise IngestLeaseLost("quota checkpoint already advanced")
            row.checked = end
            row.synced += int(result.get("synced") or 0)
            row.sync_failed += len(result.get("errors") or [])
            row.updated, row.failures, row.error = time.time(), 0, ""
            if end == row.total:
                row.status, row.owner, row.lease_until, row.payload = "completed", "", 0, "[]"
        return self.get(job_id)

    def fail(self, job_id, owner):
        with self.transaction() as session:
            row = session.query(AccountIngestJob).filter_by(id=job_id, owner=owner).with_for_update().one_or_none()
            if row is None or row.status not in {"saving", "syncing"}:
                return
            row.failures += 1
            row.error = "import_storage_error" if row.status == "saving" else "import_sync_error"
            row.status = ("queued" if row.saved < row.total else "sync_pending") if row.failures < 3 else "failed"
            row.owner, row.lease_until, row.updated = "", 0, time.time()
            row.retry_at = time.time() + min(30, 2 ** row.failures)

    def _work(self, phase):
        owner = uuid.uuid4().hex
        while not self._stop.is_set():
            job_id = None
            try:
                job_id = self.claim(phase, owner)
                if not job_id:
                    self._stop.wait(1)
                    continue
                heart_stop = threading.Event()

                def renew():
                    while not heart_stop.wait(10):
                        try:
                            if not self.heartbeat(job_id, owner):
                                return
                        except Exception:
                            # The transaction checkpoint still verifies ownership.
                            logger.warning("account import heartbeat unavailable")

                heart = threading.Thread(target=renew, name="import-heartbeat", daemon=True)
                heart.start()
                try:
                    if phase == "save":
                        self.accounts._account_snapshot_checked_at = 0
                        self.accounts._refresh_accounts_snapshot_if_stale(wait_for_refresh=True)
                    while not self._stop.is_set():
                        view = self.save_batch(job_id, owner) if phase == "save" else self.sync_batch(job_id, owner)
                        if view["status"] != ("saving" if phase == "save" else "syncing"):
                            break
                finally:
                    heart_stop.set()
                    heart.join(timeout=1)
            except IngestLeaseLost:
                pass
            except Exception as exc:
                # Never put account payloads, tokens or raw upstream errors into
                # the public job row or ingestion logs.
                logger.warning("account import worker failed (%s)", type(exc).__name__)
                if job_id:
                    try:
                        self.fail(job_id, owner)
                    except Exception:
                        pass
                self._stop.wait(1)

    def start(self):
        with self._start_lock:
            if self._threads:
                return
            self._stop.clear()
            for phase in ("save", "sync"):
                thread = threading.Thread(target=self._work, args=(phase,), name=f"account-import-{phase}", daemon=True)
                thread.start()
                self._threads.append(thread)

    def stop(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        # Unfinished leases expire and resume from committed offsets on restart.


_singleton = None
_singleton_lock = threading.Lock()


def get_account_ingest_service():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = AccountIngestService()
        return _singleton
