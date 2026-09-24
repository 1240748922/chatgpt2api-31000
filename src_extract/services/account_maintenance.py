"""Small, cooperative background batches; foreground generation is never throttled."""
from __future__ import annotations

import time
from threading import Event

from services.account_maintenance_policy import account_maintenance_decision
from services.account_maintenance_progress import account_maintenance_progress
from services.maintenance_load import configured_thresholds
from services.runtime_configuration import env_int
from utils.log import logger


def maintenance_cycle_delay(decision: dict, *, pending: bool, advanced: int,
                            next_discovery_in: float | None = None) -> float:
    """Continue a healthy queue, not failed work or unbounded pool rescans."""
    retry = configured_thresholds()[2]
    if (advanced <= 0 or not decision.get("allowed") or decision.get("mode") != "normal"
            or decision.get("policy") != "performance"):
        return retry
    gap = min(retry, env_int("CHATGPT2API_MAINTENANCE_CONTINUE_SECONDS", 2, 1, 3600))
    if pending:
        return gap
    # When a full candidate window was drained, retain its scan deadline rather
    # than adding another idle interval after the time spent processing it.
    return min(retry, max(gap, next_discovery_in)) if next_discovery_in is not None else retry


def sync_idle_batch(service, tokens: list[str], stop_event: Event) -> int:
    return _run_idle_batches(service.sync_accounts_and_quota, tokens, stop_event, "quota")


def renew_idle_batch(service, tokens: list[str], stop_event: Event) -> int:
    """Replenish ready credentials in small batches before less urgent checks."""
    return _run_idle_batches(service.renew_expiring_access_tokens, tokens, stop_event, "renewal")


def _run_idle_batches(operation, tokens: list[str], stop_event: Event, kind: str) -> int:
    # At most one small batch remains in flight if pressure rises during a check.
    started = time.monotonic()
    processed = 0
    while processed < len(tokens):
        if stop_event.is_set() or time.monotonic() - started >= 20:
            break
        decision = account_maintenance_decision()
        if not decision["allowed"]:
            break
        width = decision["batch_size"]
        batch = tokens[processed:processed + width]
        with account_maintenance_progress.batch(kind, len(batch)) as progress:
            result = operation(batch) or {}
            progress.reconcile(result)
        logger.info({
            "event": f"account_maintenance_{kind}_batch",
            "attempted": len(batch),
            "synced": result.get("synced", 0),
            "refreshed": result.get("refreshed", 0),
            "failed": len(result.get("errors") or []),
            "maintenance_mode": decision["mode"],
            "maintenance_reasons": decision["reason_codes"],
        })
        processed += len(batch)
        if decision["mode"] != "normal":
            break  # One small batch per invocation under reduced/unknown load.
    return processed
