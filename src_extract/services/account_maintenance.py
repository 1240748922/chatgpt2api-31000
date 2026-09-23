"""Small, cooperative background batches; foreground generation is never throttled."""
from __future__ import annotations

import time
from threading import Event

from services.maintenance_load import maintenance_is_allowed
from services.runtime_configuration import env_int
from utils.log import logger


def sync_idle_batch(service, tokens: list[str], stop_event: Event) -> int:
    return _run_idle_batches(service.sync_accounts_and_quota, tokens, stop_event, "quota")


def renew_idle_batch(service, tokens: list[str], stop_event: Event) -> int:
    """Replenish ready credentials in small batches before less urgent checks."""
    return _run_idle_batches(service.renew_expiring_access_tokens, tokens, stop_event, "renewal")


def _run_idle_batches(operation, tokens: list[str], stop_event: Event, kind: str) -> int:
    width = env_int("CHATGPT2API_MAINTENANCE_BATCH_CONCURRENCY", 2, 1, 8)
    # At most one small batch remains in flight if traffic rises during a check.
    started = time.monotonic()
    processed = 0
    for offset in range(0, len(tokens), width):
        if stop_event.is_set() or time.monotonic() - started >= 20:
            break
        allowed, _ = maintenance_is_allowed()
        if not allowed:
            break
        batch = tokens[offset:offset + width]
        result = operation(batch) or {}
        logger.info({
            "event": f"account_maintenance_{kind}_batch",
            "attempted": len(batch),
            "synced": result.get("synced", 0),
            "refreshed": result.get("refreshed", 0),
            "failed": len(result.get("errors") or []),
        })
        processed += len(batch)
    return processed
