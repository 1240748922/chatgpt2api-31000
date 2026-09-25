"""Capability cooldowns do not change account authentication or image quota."""
from __future__ import annotations

import time
import math
from datetime import datetime, timezone

from services.runtime_configuration import env_int

UPLOAD_COOLDOWN_FIELDS = (
    "file_upload_blocked_until", "file_upload_throttle_streak", "last_file_upload_throttled_at",
)


def upload_blocked_until(account: dict) -> float:
    try:
        until = float(account.get("file_upload_blocked_until") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return max(0.0, until) if math.isfinite(until) else 0.0


def upload_blocked(account: dict, now: float | None = None) -> bool:
    return upload_blocked_until(account) > (time.time() if now is None else now)


def upload_cooldown_seconds(retry_after: int | None) -> int:
    fallback = env_int("CHATGPT2API_UPLOAD_COOLDOWN_SECONDS", 900, 1, 86400)
    return max(1, retry_after if retry_after is not None else fallback)


def record_upload_throttle(account: dict, retry_after: int | None, now: float) -> None:
    previous_until = upload_blocked_until(account)
    duration = upload_cooldown_seconds(retry_after)
    if retry_after is None:
        try:
            previous_streak = max(0, min(16, int(account.get("file_upload_throttle_streak") or 0)))
        except (TypeError, ValueError, OverflowError):
            previous_streak = 0
        # Increase only after a cooldown has actually elapsed. Concurrent
        # failures in the same blocked window are a single observation.
        recent = previous_until > 0 and now <= previous_until + duration
        streak = max(1, previous_streak + int(now > previous_until)) if recent else 1
        maximum = max(duration, env_int("CHATGPT2API_UPLOAD_COOLDOWN_MAX_SECONDS", 7200, 1, 86400))
        duration = min(maximum, duration * (2 ** min(streak - 1, 16)))
    else:
        # An explicit upstream deadline wins over speculative backoff.
        streak = 0
    account["file_upload_throttle_streak"] = streak
    account["file_upload_blocked_until"] = max(previous_until, now + duration)
    account["last_file_upload_throttled_at"] = datetime.fromtimestamp(now, timezone.utc).isoformat()


def merge_upload_cooldown(target: dict, source: dict) -> None:
    """Carry the history belonging to the longer cooldown together with it."""
    if upload_blocked_until(source) > upload_blocked_until(target):
        for field in UPLOAD_COOLDOWN_FIELDS:
            if field in source:
                target[field] = source[field]
            else:
                target.pop(field, None)
