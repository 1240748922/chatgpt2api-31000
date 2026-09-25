"""Short, per-credential transactions; never hold a DB connection over HTTP.

RT attempts have a durable send fence. A crash/ambiguous response is NOT a
license to redeem the same RT again. No credentials are stored in this table.
Image leases cover the request deadline plus a recovery grace period.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import time
from uuid import uuid4

from sqlalchemy import Column, String, Text, text
from sqlalchemy.orm import sessionmaker

from services.application_database import DatabaseBase
from services.account_capabilities import UPLOAD_COOLDOWN_FIELDS, upload_blocked_until, record_upload_throttle


class CredentialGate(DatabaseBase):
    __tablename__ = "credential_runtime_gates"
    key = Column(String(80), primary_key=True)
    data = Column(Text, nullable=False)


class CredentialBusy(RuntimeError):
    def __init__(self, reason="credential_in_use", *, retry_after=None):
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(reason)


def generation(account):
    values = [account.get(k) or "" for k in ("access_token", "refresh_token", "last_token_refresh_at")]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def resource(account):
    rt = account.get("refresh_token")
    return ("rt:" if rt else "at:") + hashlib.sha256(str(rt or account.get("access_token") or "").encode()).hexdigest()


class LeasedToken(str):
    """Keep the public string API while releasing the exact concurrent lease."""
    def __new__(cls, token, coordinator, key, owner):
        value = super().__new__(cls, token)
        value.coordinator, value.key, value.owner = coordinator, key, owner
        return value

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def release(self, *, upload_cooldown_seconds=0, upload_retry_after_missing=False):
        # The DB deletion is idempotent, including retries after a lost commit.
        # Retain a failed publication on this exact ticket if cleanup is retried.
        self.upload_cooldown_seconds = max(getattr(self, "upload_cooldown_seconds", 0), upload_cooldown_seconds)
        self.upload_retry_after_missing = getattr(self, "upload_retry_after_missing", False) or upload_retry_after_missing
        for attempt in range(3):
            try:
                return self.coordinator.release(
                    self.key, self.owner, upload_cooldown_seconds=self.upload_cooldown_seconds,
                    upload_retry_after_missing=self.upload_retry_after_missing,
                )
            except CredentialBusy:
                # A simultaneous finish briefly owns this credential gate.
                # Retry only this nonblocking lock miss, at most 30ms of sleep;
                # database/network failures retain the original lease expiry.
                if attempt == 2:
                    raise
                time.sleep(0.01 * (attempt + 1))


class CredentialCoordinator:
    def __init__(self, storage):
        self.storage = storage
        self.engine = storage.engine
        CredentialGate.__table__.create(self.engine, checkfirst=True)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    @contextmanager
    def edit(self, key):
        with self.Session() as session:
            if self.engine.dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            else:
                # Immediate contention result: no credential lock queue on the
                # image hot path. Hash collisions only cause a harmless skip.
                session.execute(text("SET LOCAL statement_timeout = '1500ms'"))
                if not session.execute(text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"), {"key": key}).scalar():
                    raise CredentialBusy()
            row = session.get(CredentialGate, key)
            data = json.loads(row.data) if row else {}
            # Use DB time across replicas, not container clock offsets.
            now = (session.execute(text("SELECT extract(epoch from clock_timestamp())")).scalar()
                   if self.engine.dialect.name == "postgresql" else time.time())
            now = float(now)
            data["leases"] = {owner: until for owner, until in data.get("leases", {}).items() if until > now}
            yield session, data, now
            if row is None:
                session.add(CredentialGate(key=key, data=json.dumps(data)))
            else:
                row.data = json.dumps(data)
            session.commit()

    def current(self, session, account):
        from services.storage.database_storage import AccountModel
        row = session.query(AccountModel).filter_by(access_token=str(account["access_token"])).one_or_none()
        current = json.loads(row.data) if row else None
        if not current or generation(current) != generation(account):
            raise CredentialBusy("credential_changed")
        return current

    def acquire_image(self, account, *, minimum_validity, duration, limit, requires_upload=False):
        from services.account_readiness import credential_readiness
        if not isinstance(account, dict) or not account.get("access_token"):
            raise CredentialBusy("credential_changed")
        key, owner = resource(account), uuid4().hex
        with self.edit(key) as (session, data, now):
            current = self.current(session, account)
            # The account result writer is asynchronous. Consult the same gate
            # that releases the previous lease before admitting another upload.
            until = max(upload_blocked_until(current), upload_blocked_until(data))
            if requires_upload and until > now:
                raise CredentialBusy("file_upload_throttled", retry_after=math.ceil(until - now))
            if (credential_readiness(current, minimum_validity, now=now)["state"] != "ready"
                    or current.get("status") == "限流"):
                raise CredentialBusy("credential_not_ready")
            state = data.get("refresh_state")
            if state in {"sent", "uncertain"} or (
                state == "done" and data.get("generation") == generation(account)
            ):
                raise CredentialBusy("credential_refresh_pending")
            if len(data["leases"]) >= limit:
                raise CredentialBusy()
            data["leases"][owner] = now + duration
        return LeasedToken(account["access_token"], self, key, owner)

    def release(self, key, owner, *, upload_cooldown_seconds=0, upload_retry_after_missing=False):
        with self.edit(key) as (_, data, now):
            # Publish and free capacity atomically. An overlapping success or
            # duplicate cleanup must never erase/extend this cooldown. Only the
            # live lease owner can report a new upload failure.
            if owner in data["leases"] and upload_cooldown_seconds > 0:
                record_upload_throttle(data, None if upload_retry_after_missing else upload_cooldown_seconds, now)
            data["leases"].pop(owner, None)
            cooldown = {field: data[field] for field in UPLOAD_COOLDOWN_FIELDS if field in data}
        return cooldown

    def begin_refresh(self, account, *, imported=False):
        key, owner = resource(account), uuid4().hex
        with self.edit(key) as (session, data, _now):
            if not imported:
                self.current(session, account)
            if data["leases"]:
                raise CredentialBusy()
            state = data.get("refresh_state")
            if state in {"sent", "uncertain"}:
                raise CredentialBusy("refresh_outcome_uncertain")
            if state == "done" and (imported or data.get("generation") == generation(account)):
                raise CredentialBusy("refresh_already_exchanged")
            data.update(refresh_state="sent", owner=owner, generation=generation(account), started_at=_now)
        return key, owner

    def blocked_states(self):
        # One cached inventory scan, not a SELECT per displayed account.
        with self.Session() as session:
            result = {}
            for key, raw in session.query(CredentialGate.key, CredentialGate.data):
                data = json.loads(raw)
                state = data.get("refresh_state")
                if state in {"sent", "uncertain"}:
                    result[key] = "refreshing" if state == "sent" and time.time() - data.get("started_at", 0) < 120 else "uncertain"
            return result

    def finish_refresh(self, ticket, state):
        key, owner = ticket
        with self.edit(key) as (_, data, _now):
            if data.get("owner") != owner:
                raise CredentialBusy("refresh_fence_lost")
            data["refresh_state"] = state

