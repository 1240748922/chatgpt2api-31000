"""Best-effort extra archival, isolated from response deadlines and billing.

Only decoded bytes enter the bounded queue; no account/session is retained.
Receipts live in the shared database so any replica can enrich admin logs.
Pending bytes are not crash-durable: keep the upstream conversation and expose
expired/missing receipts as interrupted, never pretend an image was saved.
"""
from __future__ import annotations

import threading
import time
from uuid import uuid4

from services.application_database import initialize_application_database, resolve_database_url
from services.bounded_task_runner import BoundedTaskRunner
from services.storage.image_archive_repository import save_receipt, load_receipts
from utils.image_tokens import image_size_from_bytes
from utils.log import logger


class ImageArchiveService:
    def __init__(self, *, database_url=None, max_workers=1, queue_size=16,
                 max_bytes=64 * 1024 * 1024, max_age_seconds=300):
        self.database_url = database_url
        self.max_bytes = max_bytes
        self.max_age_seconds = max_age_seconds
        self._lock = threading.Lock()
        self._bytes = 0
        self.runner = BoundedTaskRunner(name="image-extra-archive",
                                       max_workers=max_workers, queue_size=queue_size)
        self._engine = None

    def start(self):
        self.runner.start()

    def shutdown(self):
        self.runner.shutdown_cancel_pending_and_wait()

    def submit(self, image: bytes, base_url, *, slot: int, call_id: str, save) -> dict:
        entry = {"archive_id": uuid4().hex, "slot": slot, "status": "pending",
                 "expires_at": time.time() + self.max_age_seconds + 120}
        image_bytes = len(image)
        with self._lock:
            if self._bytes + image_bytes > self.max_bytes:
                return {**entry, "status": "failed", "reason": "archive_memory_limit"}
            reservation = self.runner.reserve()
            if reservation is None:
                return {**entry, "status": "failed", "reason": "archive_queue_full"}
            self._bytes += image_bytes
        released = False
        def release():
            nonlocal released, image
            with self._lock:
                if not released:
                    self._bytes -= image_bytes
                    # An idle worker may retain its last callback until the
                    # next job; do not retain that callback's large byte buffer.
                    image = b""
                    released = True
        def persist(result):
            try:
                if self._engine is None:
                    self._engine = initialize_application_database(self.database_url or resolve_database_url())
                save_receipt(self._engine, entry["archive_id"], result)
            except Exception as exc:
                logger.warning({"event": "image_extra_receipt_failed", "call_id": call_id,
                                "archive_id": entry["archive_id"], "error_type": type(exc).__name__})
        queued = time.monotonic()
        def run():
            result = {**entry, "status": "failed"}
            try:
                if time.monotonic() - queued >= self.max_age_seconds:
                    result["reason"] = "archive_queue_expired"
                else:
                    url = save(image, base_url, deadline_monotonic=time.monotonic() + 60)
                    if not url:
                        raise RuntimeError("extra image storage returned no URL")
                    result.update(status="saved", url=url)
                    dimensions = image_size_from_bytes(image)
                    if dimensions:
                        result["width"], result["height"] = dimensions
            except Exception as exc:
                result["reason"] = "archive_storage_failed"
                logger.warning({"event": "image_extra_storage_failed", "call_id": call_id,
                                "archive_id": entry["archive_id"], "error_type": type(exc).__name__})
            finally:
                try:
                    persist(result)
                finally:
                    release()
        def cancel(_error):
            try:
                persist({**entry, "status": "failed", "reason": "archive_shutdown"})
            finally:
                release()
        try:
            committed = reservation.commit(run, on_cancel=cancel)
        except Exception:
            reservation.rollback()
            release()
            raise
        if not committed:
            release()
            return {**entry, "status": "failed", "reason": "archive_shutdown"}
        return entry


def enrich_archive_detail(detail: dict, engine) -> dict:
    extras = [item for item in (detail.get("extra_images") or []) if isinstance(item, dict)]
    pending = [item for item in extras if isinstance(item, dict) and item.get("status") == "pending"
               and item.get("archive_id")]
    if not pending:
        return detail
    try:
        receipts = load_receipts(engine, [item["archive_id"] for item in pending])
    except Exception as exc:
        # Optional archive metadata must not turn a valid request log into 500.
        logger.warning({"event": "image_extra_receipt_read_failed",
                        "error_type": type(exc).__name__})
        return {**detail, "image_archive_status_error": "归档状态暂时读取失败，请刷新重试"}
    result = dict(detail)
    resolved = []
    saved = failed = 0
    for item in extras:
        value = dict(item)
        if item.get("status") == "pending" and item.get("archive_id"):
            value = dict(receipts.get(item["archive_id"], item))
            if value.get("status") == "pending" and time.time() >= float(value.get("expires_at") or 0):
                value.update(status="failed", reason="archive_interrupted")
            saved += value.get("status") == "saved"
            failed += value.get("status") == "failed"
        resolved.append(value)
    result["extra_images"] = resolved
    result["image_extra_saved_count"] = int(result.get("image_extra_saved_count") or 0) + saved
    result["image_saved_count"] = int(result.get("image_saved_count") or 0) + saved
    result["image_extra_failed_count"] = int(result.get("image_extra_failed_count") or 0) + failed
    result["image_extra_pending_count"] = len(pending) - saved - failed
    return result


image_archive_service = ImageArchiveService()
