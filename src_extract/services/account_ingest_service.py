"""Durable, bounded account ingestion; no image workers are used.

The save checkpoint commits in the SAME transaction as account rows. A crash
cannot replay an acknowledged batch or report rows saved before they exist.
Quota checking and RT exchange have separate workers from AT ingestion.
"""
from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import threading
import time
import uuid

from sqlalchemy import Column, Float, Integer, String, Text, or_, and_, text, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from services.application_database import DatabaseBase, initialize_application_database, resolve_database_url
from services.storage.database_storage import account_commit_hook
from services.runtime_configuration import env_int
from services.account_import_credentials import extract_import_credentials
from services.account_processing import account_import_worker_count, account_import_slot, account_quota_sync_worker_count, bounded_future_results

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


class AccountIngestChunk(DatabaseBase):
    """Bounded payload reads, also compatible with jobs from before this table."""
    __tablename__ = "account_ingest_chunks"
    job_id = Column(String(40), primary_key=True)
    offset = Column(Integer, primary_key=True)
    end = Column(Integer, nullable=False)
    refresh_total = Column(Integer, nullable=False, default=0)
    payload = Column(Text, nullable=False)


class AccountIngestRefresh(DatabaseBase):
    __tablename__ = "account_ingest_refresh_results"
    job_id = Column(String(40), primary_key=True)
    offset = Column(Integer, primary_key=True)
    payload = Column(Text, nullable=False)  # rotated RT persisted before account save
    error = Column(String(80), nullable=False, default="")


class AccountIngestEvent(DatabaseBase):
    __tablename__ = "account_ingest_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(40), nullable=False, index=True)
    created = Column(Float, nullable=False)
    code = Column(String(80), nullable=False)
    details = Column(Text, nullable=False)  # only locally constructed counts/codes


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

    def public(self, row, session, *, include_result=False):
        refresh_total = session.query(func.sum(AccountIngestChunk.refresh_total)).filter_by(job_id=row.id).scalar() or 0
        refresh_done = session.query(AccountIngestRefresh).filter_by(job_id=row.id).count()
        failures = session.query(AccountIngestRefresh).filter_by(job_id=row.id).filter(AccountIngestRefresh.error != "")
        failed = failures.count()
        failed_saved = failures.filter(AccountIngestRefresh.offset < row.saved).count()
        result = {
            "id": row.id, "status": row.status, "total": row.total,
            "processed": row.saved, "saved": row.saved-failed_saved, "added": row.added,
            "skipped": max(0, row.saved - row.added-failed_saved) + row.input_duplicates,
            "refresh_total": refresh_total, "refresh_done": refresh_done, "refresh_failed": failed,
            "checked": row.checked, "synced": row.synced, "sync_failed": row.sync_failed,
            "sync_after_import": bool(row.sync_after), "error_code": row.error,
            "created_at": row.created, "updated_at": row.updated,
            "done": row.status in {"completed", "failed"},
        }
        if include_result and result["done"]:
            result["updated_ids"] = json.loads(row.updated_ids)
        return result

    @staticmethod
    def _event(session, job_id, code, **details):
        session.add(AccountIngestEvent(job_id=job_id, created=time.time(), code=code,
                                       details=json.dumps(details, ensure_ascii=False)))

    def events(self, job_id, after=0, limit=100):
        with self.Session() as session:
            rows = session.query(AccountIngestEvent).filter_by(job_id=job_id).filter(
                AccountIngestEvent.id > after).order_by(AccountIngestEvent.id).limit(limit).all()
            return [{"id": r.id, "time": r.created, "code": r.code, **json.loads(r.details)} for r in rows]

    def submit(self, items, *, sync_after_import=False, request_key=None):
        if not items or len(items) > 50000:
            raise ValueError("每个任务需要 1～50000 个账号")
        # Deduplicate once, before taking any account-pool lock. Do not reset
        # existing lifecycle/quota state on re-import: this is a replenish path.
        unique = {}
        for index, item in enumerate(items, 1):
            credentials = extract_import_credentials(item)
            at = credentials.get("access_token", "")
            # Older clients wrapped all lines in access_token, including RTs.
            if at.startswith("rt."):
                credentials.pop("access_token")
                credentials.setdefault("refresh_token", at)
            if not credentials.get("access_token") and not credentials.get("refresh_token"):
                raise ValueError(f"第 {index} 条缺少 AT 或 RT")
            normalized = {k: v for k, v in item.items() if k not in {
                "credentials", "credential", "tokens", "auth", "accessToken", "refreshToken", "idToken", "token", "access_token"}}
            normalized.update(credentials)
            prepared = self.accounts._prepare_account_payload(normalized) if credentials.get("access_token") else normalized
            key = ("at", credentials["access_token"]) if credentials.get("access_token") else ("rt", credentials["refresh_token"])
            unique[key] = prepared
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
                    return self.public(existing, session)
                if session.query(AccountIngestJob).filter(AccountIngestJob.status.notin_(["completed", "failed"])).count() >= 100:
                    raise IngestConflict("导入队列已满，请稍后再提交")
                values = list(unique.values())
                has_refresh = any(not item.get("access_token") for item in values)
                row = AccountIngestJob(
                    id=uuid.uuid4().hex, request_key=key, fingerprint=fingerprint,
                    status="refresh_pending" if has_refresh else "queued", created=now, updated=now, total=len(unique),
                    input_duplicates=len(items)-len(unique), sync_after=int(bool(sync_after_import)),
                    payload="chunked:rt" if has_refresh else "chunked:at", updated_ids="[]", owner="", lease_until=0, retry_at=0,
                    saved=0, added=0, checked=0, synced=0, sync_failed=0, failures=0, error="",
                )
                session.add(row)
                chunks = [{"job_id": row.id, "offset": offset, "end": min(len(values), offset+self.batch_size),
                           "refresh_total": sum(not item.get("access_token") for item in values[offset:offset+self.batch_size]),
                           "payload": json.dumps(values[offset:offset+self.batch_size], ensure_ascii=False)}
                          for offset in range(0, len(values), self.batch_size)]
                session.execute(AccountIngestChunk.__table__.insert(), chunks)
                self._event(session, row.id, "submitted", total=len(values), duplicates=row.input_duplicates,
                            refresh_total=sum(c["refresh_total"] for c in chunks))
                session.flush()
                return self.public(row, session)
        except IntegrityError:
            # Concurrent identical POSTs race on a unique key, never create two jobs.
            with self.Session() as session:
                row = session.query(AccountIngestJob).filter_by(request_key=key).one()
                if row.fingerprint != fingerprint:
                    raise IngestConflict("同一幂等键不能提交不同内容")
                return self.public(row, session)

    def get(self, job_id, *, include_result=False):
        with self.Session() as session:
            row = session.get(AccountIngestJob, job_id)
            return self.public(row, session, include_result=include_result) if row else None

    def list_jobs(self, limit=30):
        with self.Session() as session:
            rows = session.query(AccountIngestJob).order_by(AccountIngestJob.created.desc()).limit(min(100, max(1, limit))).all()
            return [self.public(row, session) for row in rows]

    def retry(self, job_id):
        with self.transaction() as session:
            row = session.query(AccountIngestJob).filter_by(id=job_id).with_for_update().one_or_none()
            if row is None:
                return None
            if row.status != "failed":
                raise IngestConflict("只有失败任务可以重试")
            row.status = "sync_pending" if row.saved == row.total else "refresh_pending" if row.payload == "chunked:rt" else "queued"
            row.owner, row.lease_until, row.retry_at, row.failures, row.error = "", 0, 0, 0, ""
            row.updated = time.time()
            self._event(session, row.id, "retry_requested", processed=row.saved)
            return self.public(row, session)

    def claim(self, phase, owner):
        pending, running = {"save": ("queued", "saving"), "refresh": ("refresh_pending", "refreshing"), "sync": ("sync_pending", "syncing")}[phase]
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
            self._event(session, row.id, "phase_started", phase=phase, processed=row.saved)
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
                AccountIngestJob.status.in_(["saving", "refreshing", "syncing"]),
                AccountIngestJob.lease_until >= time.time(),
            ).update({"lease_until": time.time()+self.LEASE_SECONDS}, synchronize_session=False)

    @staticmethod
    def _finish_save(row, end, new_ids):
        previous_ids = json.loads(row.updated_ids)
        previous_set = set(previous_ids)
        ids = list(dict.fromkeys([*previous_ids, *new_ids]))
        new_unique = [account_id for account_id in ids if account_id not in previous_set]
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

    def _batch_items(self, session, row, start, size):
        chunk = session.query(AccountIngestChunk).filter_by(job_id=row.id).filter(
            AccountIngestChunk.offset <= start, AccountIngestChunk.end > start).first()
        if chunk is None:  # already queued on an older release
            end = min(row.total, start+size)
            return end, json.loads(row.payload)[start:end]
        end = min(chunk.end, start+size)
        items = json.loads(chunk.payload)[start-chunk.offset:end-chunk.offset]
        results = session.query(AccountIngestRefresh).filter_by(job_id=row.id).filter(
            AccountIngestRefresh.offset >= start, AccountIngestRefresh.offset < end).all()
        for result in results:
            items[result.offset-start] = None if result.error else json.loads(result.payload)
        return end, items

    @staticmethod
    def _clear_payloads(session, job_id):
        session.query(AccountIngestChunk).filter_by(job_id=job_id).update({"payload": "[]"}, synchronize_session=False)
        session.query(AccountIngestRefresh).filter_by(job_id=job_id).update({"payload": "{}"}, synchronize_session=False)

    @staticmethod
    def _refresh_error(exc):
        # Never publish upstream text: it may echo a credential or proxy URL.
        from services.account_service import TerminalRefreshTokenError, OAuthRefreshError
        from services.credential_coordinator import CredentialBusy
        if isinstance(exc, CredentialBusy):
            return exc.reason
        if isinstance(exc, TerminalRefreshTokenError):
            return "refresh_token_invalid"
        if isinstance(exc, OAuthRefreshError):
            return "refresh_rate_limited" if exc.status_code == 429 else "refresh_upstream_error"
        return "refresh_network_error"

    @staticmethod
    def _import_account_label(item, number):
        """Return the only identity allowed in import progress events."""
        def find_label(value):
            if not isinstance(value, dict):
                return ""
            for key in ("email", "account_email", "username"):
                label = str(value.get(key) or "").strip()
                if label:
                    return label
            for key in ("profile", "user", "account", "credentials", "credential", "auth"):
                nested = value.get(key)
                label = find_label(nested)
                if label:
                    return label
            return ""

        if isinstance(item, dict):
            label = find_label(item)
            if label:
                return label[:240]
        return f"账号 #{max(1, int(number or 0))}"

    @classmethod
    def _save_event_items(cls, items, inserted_tokens):
        inserted_tokens = set(inserted_tokens or ())
        projected = []
        for index, item in enumerate(items or (), 1):
            token = str(item.get("access_token") or "") if isinstance(item, dict) else ""
            added = token in inserted_tokens
            projected.append({
                "index": index,
                "account_label": cls._import_account_label(item, index),
                "status": "success" if added else "skipped",
                "stage": "save",
                "message": "账号已入库" if added else "账号已存在，跳过重复写入",
                "error_code": "",
            })
        return projected

    def _prepare_refresh_batch(self, job_id, owner, start, items):
        pending = [(start+i, item) for i, item in enumerate(items) if item and not item.get("access_token")]
        if not pending:
            return
        workers = account_import_worker_count(len(pending))
        # One O(pool) snapshot per RT batch, not per RT/network call. Existing
        # RT imports must not rotate an already managed account behind its back.
        with self.accounts._lock:
            existing_rows = tuple(self.accounts._accounts.values())
        existing_rt = {item["refresh_token"]: item for item in existing_rows if item.get("refresh_token")}

        def exchange(entry):
            began = time.monotonic()
            ticket = None
            coordinator = self.accounts._credential_coordinator
            known = existing_rt.get(entry[1]["refresh_token"])
            if known is not None:
                return dict(known), "", 0, None
            try:
                if self.accounts._strict_admission():
                    ticket = coordinator.begin_refresh(entry[1], imported=True)
                updated = self.accounts._request_access_token_refresh(entry[1]["refresh_token"], entry[1], request_slot=account_import_slot)
                prepared = self.accounts._prepare_account_payload({**entry[1], **updated})
                if prepared is None:
                    raise ValueError("missing access token")
                return prepared, "", int((time.monotonic()-began)*1000), ticket
            except Exception as exc:
                if ticket:
                    from services.account_service import OAuthRefreshError
                    safe = isinstance(exc, OAuthRefreshError) and exc.status_code in {400, 401, 403, 429}
                    coordinator.finish_refresh(ticket, "idle" if safe else "uncertain")
                return None, self._refresh_error(exc), int((time.monotonic()-began)*1000), None

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="import-rt") as executor:
            for future, entry in bounded_future_results(executor, exchange, pending, max_in_flight=workers):
                prepared, error, duration, ticket = future.result()
                with self.transaction() as session:
                    self._owned(session, job_id, owner, "refreshing", start)
                    session.add(AccountIngestRefresh(job_id=job_id, offset=entry[0],
                                payload=json.dumps(prepared or {}, ensure_ascii=False), error=error))
                    self._event(session, job_id, "refresh_failed" if error else "refresh_done",
                                item=entry[0]+1, account_label=self._import_account_label(entry[1], entry[0]+1),
                                error_code=error, duration_ms=duration,
                                items=[{
                                    "index": entry[0]+1,
                                    "account_label": self._import_account_label(entry[1], entry[0]+1),
                                    "status": "failed" if error else "success",
                                    "stage": "refresh",
                                    "message": error or ("复用已有账号凭据" if entry[1]["refresh_token"] in existing_rt else "RT 已兑换"),
                                    "error_code": error or "",
                                }])
                if ticket:
                    # Mark consumed only after the durable credential-bearing
                    # import checkpoint exists. Crash before here stays fenced.
                    self.accounts._credential_coordinator.finish_refresh(ticket, "done")

    def save_batch(self, job_id, owner, *, refresh=False):
        status = "refreshing" if refresh else "saving"
        # Verify ownership before any RT call; network work never holds a DB lock.
        with self.transaction() as session:
            row = self._owned(session, job_id, owner, status)
            start = row.saved
            size = min(self.batch_size, account_import_worker_count(self.batch_size)*2) if refresh else self.batch_size
            end, items = self._batch_items(session, row, start, size)
        if refresh:
            self._prepare_refresh_batch(job_id, owner, start, items)
            with self.Session() as session:
                row = session.get(AccountIngestJob, job_id)
                end, items = self._batch_items(session, row, start, size)
        items = [item for item in items if item is not None]
        began = time.monotonic()
        committed = False

        def finish(session, row, ids, event_items=None):
            self._finish_save(row, end, ids)
            self._event(session, job_id, "batch_saved", start=start+1, end=end,
                        saved=len(items), added=len(ids), duration_ms=int((time.monotonic()-began)*1000),
                        items=event_items if event_items is not None else self._save_event_items(items, ids))
            if row.status == "completed":
                self._clear_payloads(session, job_id)
                self._event(session, job_id, "completed")

        def checkpoint(session, stage, mutation, counts):
            nonlocal committed
            row = self._owned(session, job_id, owner, status, start)
            if stage == "after":
                # Create-only batches never alter existing account rows. IDs
                # are committed with the rows, not reconstructed after a crash.
                inserted_keys = set(counts["inserted_keys"])
                ids = [item["management_id"] for item in mutation.upserts if item["access_token"] in inserted_keys]
                finish(session, row, ids, self._save_event_items(items, inserted_keys))
                committed = True

        with account_commit_hook(checkpoint, self.accounts.storage):
            self.accounts.add_account_items(items, return_items=False, skip_existing=True)
        # A batch containing only existing/rotated tokens requires no account
        # write. Advancing its checkpoint alone is then safe and idempotent.
        if not committed:
            with self.transaction() as session:
                row = self._owned(session, job_id, owner, status, start)
                finish(session, row, [], self._save_event_items(items, set()))
        return self.get(job_id)

    def sync_batch(self, job_id, owner):
        with self.Session() as session:
            row = session.get(AccountIngestJob, job_id)
            start = row.checked
            end, items = self._batch_items(session, row, start, max(10, account_quota_sync_worker_count(row.total)*2))
            tokens = [item["access_token"] for item in items if item is not None]
        began = time.monotonic()
        result = self.accounts.sync_accounts_and_quota(tokens, include_items=False, include_results=True)
        synced = min(len(tokens), max(0, int(result.get("synced") or 0)))
        failed = len(tokens)-synced
        with self.transaction() as session:
            row = self._owned(session, job_id, owner, "syncing")
            if row.checked != start:
                raise IngestLeaseLost("quota checkpoint already advanced")
            row.checked = end
            row.synced += synced
            row.sync_failed += failed
            row.updated, row.failures, row.error = time.time(), 0, ""
            self._event(session, job_id, "quota_batch", start=start+1, end=end,
                        synced=synced, failed=failed,
                        duration_ms=int((time.monotonic()-began)*1000),
                        items=list(result.get("results") or []))
            for error in result.get("errors") or []:
                code = error.get("failure_code")
                safe_code = code if code in {"auth_invalid", "file_upload_throttled", "image_quota_exhausted", "no_available_account"} else "quota_sync_failed"
                self._event(session, job_id, "quota_failed", start=start+1, end=end, error_code=safe_code)
            if end == row.total:
                row.status, row.owner, row.lease_until, row.payload = "completed", "", 0, "[]"
                self._clear_payloads(session, job_id)
                self._event(session, job_id, "completed")
        return self.get(job_id)

    def fail(self, job_id, owner):
        with self.transaction() as session:
            row = session.query(AccountIngestJob).filter_by(id=job_id, owner=owner).with_for_update().one_or_none()
            if row is None or row.status not in {"saving", "refreshing", "syncing"}:
                return
            row.failures += 1
            row.error = "import_sync_error" if row.status == "syncing" else "import_storage_error"
            pending = "sync_pending" if row.saved == row.total else "refresh_pending" if row.payload == "chunked:rt" else "queued"
            row.status = pending if row.failures < 3 else "failed"
            self._event(session, job_id, "job_failed" if row.status == "failed" else "retry_scheduled", error_code=row.error, failures=row.failures)
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
                    if phase in {"save", "refresh"}:
                        self.accounts._account_snapshot_checked_at = 0
                        self.accounts._refresh_accounts_snapshot_if_stale(wait_for_refresh=True)
                    while not self._stop.is_set():
                        view = self.sync_batch(job_id, owner) if phase == "sync" else self.save_batch(job_id, owner, refresh=phase == "refresh")
                        if view["status"] != {"save": "saving", "refresh": "refreshing", "sync": "syncing"}[phase]:
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
            for phase in ("save", "refresh", "sync"):
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
