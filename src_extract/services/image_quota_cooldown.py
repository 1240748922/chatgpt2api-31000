"""Persistent cooldown for accounts confirmed to have exhausted image quota."""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

from services.runtime_configuration import env_int


IMAGE_QUOTA_COOLDOWN_FIELDS = (
    "image_quota_cooldown_until",
    "image_quota_exhausted_at",
    "image_quota_exhausted_streak",
    "image_quota_recovered_at",
)


def _timestamp(value: object) -> float:
    try:
        timestamp = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        timestamp = 0.0
    if math.isfinite(timestamp) and timestamp > 0:
        return timestamp
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return max(0.0, parsed.timestamp())
    except (ValueError, OverflowError, OSError):
        return 0.0


def image_quota_cooldown_until(account: dict) -> float:
    return _timestamp(account.get("image_quota_cooldown_until"))


def image_quota_needs_verification(account: dict) -> bool:
    exhausted_at = account.get("image_quota_exhausted_at")
    if not exhausted_at:
        return False
    exhausted = _timestamp(exhausted_at)
    recovered = _timestamp(account.get("image_quota_recovered_at"))
    return exhausted > 0 and exhausted >= recovered


def image_quota_cooling(account: dict, now: float | None = None) -> bool:
    return image_quota_needs_verification(account) and image_quota_cooldown_until(account) > (
        time.time() if now is None else now
    )


def image_quota_cooldown_seconds(retry_after: int | float | None) -> int:
    fallback = env_int("CHATGPT2API_IMAGE_QUOTA_COOLDOWN_SECONDS", 300, 1, 86400)
    if retry_after is None:
        return fallback
    try:
        return max(1, min(86400, int(float(retry_after))))
    except (TypeError, ValueError, OverflowError):
        return fallback


def record_image_quota_exhaustion(
    account: dict,
    retry_after: int | float | None,
    now: float,
) -> None:
    previous_until = image_quota_cooldown_until(account)
    duration = image_quota_cooldown_seconds(retry_after)
    try:
        previous_streak = max(0, min(16, int(account.get("image_quota_exhausted_streak") or 0)))
    except (TypeError, ValueError, OverflowError):
        previous_streak = 0

    if retry_after is None:
        # Repeated failures after a completed cooldown receive bounded
        # exponential backoff. Failures during one active window are one
        # observation, so concurrent requests do not multiply the deadline.
        recent = previous_until > 0 and now <= previous_until + duration
        streak = max(1, previous_streak + int(now > previous_until)) if recent else 1
        maximum = max(
            duration,
            env_int("CHATGPT2API_IMAGE_QUOTA_COOLDOWN_MAX_SECONDS", 3600, 1, 86400),
        )
        duration = min(maximum, duration * (2 ** min(streak - 1, 16)))
    else:
        # An explicit upstream deadline is authoritative.
        streak = 0

    account["image_quota_exhausted_streak"] = streak
    account["image_quota_cooldown_until"] = max(previous_until, now + duration)
    account["image_quota_exhausted_at"] = datetime.fromtimestamp(now, timezone.utc).isoformat()


def clear_image_quota_cooldown(account: dict, *, success: bool = False) -> None:
    if not account.get("image_quota_exhausted_at"):
        return
    # Keep a recovery event so stale replicas cannot resurrect an old failure.
    # A quota check preserves the backoff history; an actual image resets it.
    account["image_quota_recovered_at"] = time.time()
    if success:
        account["image_quota_exhausted_streak"] = 0
        account["image_quota_cooldown_until"] = None


def merge_image_quota_cooldown(target: dict, local: dict, remote: dict) -> None:
    """Merge whole observations, including clears, instead of independent fields."""
    if not any(row.get("image_quota_exhausted_at") or row.get("image_quota_recovered_at")
               for row in (local, remote)):
        return
    rows = (local, remote)
    recovered = max(_timestamp(row.get("image_quota_recovered_at")) for row in rows)
    failures = [row for row in rows
                if _timestamp(row.get("image_quota_exhausted_at")) > 0
                and _timestamp(row.get("image_quota_exhausted_at")) >= recovered]
    winner = max(failures, key=image_quota_cooldown_until) if failures else max(
        rows, key=lambda row: _timestamp(row.get("image_quota_recovered_at")),
    )
    for field in IMAGE_QUOTA_COOLDOWN_FIELDS:
        if field in winner:
            target[field] = winner[field]
        else:
            target.pop(field, None)
    if failures:
        target["image_quota_exhausted_at"] = max(
            (row.get("image_quota_exhausted_at") for row in failures), key=_timestamp,
        )
    if recovered:
        target["image_quota_recovered_at"] = recovered
