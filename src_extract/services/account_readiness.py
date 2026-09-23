"""Read-only image credential readiness; unknown expiry is not proven validity.

This projection does not mutate credentials or change the dispatch policy. It is
the rollout inventory for strict admission, independent of quota and occupancy.
"""
from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timezone

from services.account_credentials import access_token_timestamps


READINESS_LABELS = {
    "ready": "凭据就绪",
    "unknown": "有效期未知",
    "expiring": "有效期不足",
    "expired": "AT 已过期",
    "quarantined": "鉴权待核验",
    "invalid": "确认异常",
    "disabled": "已禁用",
}


def credential_readiness(account: dict, minimum_validity_seconds: float, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    token = str(account.get("access_token") or "").strip()
    _, expires_at = access_token_timestamps(token)
    if expires_at is not None and expires_at >= 253402300800:
        expires_at = None
    remaining = expires_at - now if expires_at is not None else None
    remote = str(account.get("last_remote_check_result") or "").lower()
    if account.get("status") == "禁用":
        state = "disabled"
    elif remote == "pending":
        state = "quarantined"
    elif account.get("status") == "异常" or remote == "invalid" or not token:
        state = "invalid"
    elif remaining is None:
        state = "unknown"
    elif remaining <= 0:
        state = "expired"
    elif remaining <= minimum_validity_seconds:
        state = "expiring"
    else:
        state = "ready"
    recoverable = bool(account.get("refresh_token")) and not bool(account.get("refresh_token_invalid_at"))
    return {
        "state": state,
        "label": READINESS_LABELS[state],
        "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).isoformat() if expires_at is not None else None,
        "remaining_seconds": int(remaining) if remaining is not None else None,
        "has_refresh_path": recoverable,
    }


def summarize_readiness(accounts: list[dict], minimum_validity_seconds: float, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    projections = (credential_readiness(account, minimum_validity_seconds, now=now) for account in accounts)
    return summarize_projections(projections, minimum_validity_seconds, now=now)


def summarize_projections(projections, minimum_validity_seconds: float, *, now: float) -> dict:
    """Reuse the same decoded projection for inventory and candidate counts."""
    counts = Counter({state: 0 for state in READINESS_LABELS})
    renewable = manual = 0
    for item in projections:
        counts[item["state"]] += 1
        if item["state"] in {"unknown", "expired", "expiring", "quarantined", "invalid"}:
            renewable += int(item["has_refresh_path"])
            manual += int(not item["has_refresh_path"])
    return {
        "total": sum(counts.values()), "counts": dict(counts), "labels": dict(READINESS_LABELS),
        "refresh_candidates": renewable, "needs_credentials": manual,
        "minimum_validity_seconds": int(minimum_validity_seconds),
        "sampled_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "scope": "credential_inventory", "policy": "readiness_preview",
    }
