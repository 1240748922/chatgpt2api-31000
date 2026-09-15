"""Small, cooperative background batches; foreground generation is never throttled."""
from __future__ import annotations

import time
from threading import Event

from services.maintenance_load import maintenance_is_allowed
from services.runtime_configuration import env_int
from utils.log import logger


def sync_idle_batch(service, tokens: list[str], stop_event: Event) -> int:
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
        result = service.sync_accounts_and_quota(batch) or {}
        logger.info({
            "event": "account_maintenance_quota_batch",
            "attempted": len(batch),
            "synced": result.get("synced", 0),
            "failed": len(result.get("errors") or []),
        })
        processed += len(batch)
    return processed
