"""Capability cooldowns do not change account authentication or image quota."""
from __future__ import annotations

import time
import math
from datetime import datetime, timezone

from services.runtime_configuration import env_int


def upload_blocked(account: dict, now: float | None = None) -> bool:
    try:
        until = float(account.get("file_upload_blocked_until") or 0)
    except (TypeError, ValueError):
        return False
    return until > (time.time() if now is None else now)


def record_upload_throttle(account: dict, retry_after: int | None, now: float) -> None:
    fallback = env_int("CHATGPT2API_UPLOAD_COOLDOWN_SECONDS", 900, 1, 86400)
    # Keep an existing longer cooldown when overlapping requests finish.
    until = now + max(1, retry_after if retry_after is not None else fallback)
    try:
        existing = float(account.get("file_upload_blocked_until") or 0)
    except (TypeError, ValueError):
        existing = 0.0
    account["file_upload_blocked_until"] = max(existing if math.isfinite(existing) else 0.0, until)
    account["last_file_upload_throttled_at"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
