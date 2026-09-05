from __future__ import annotations

import hashlib
import os


DEFAULT_THREAD_TOKENS = 120


def env_int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(str(os.getenv(name, "") or default).strip())
    except (TypeError, ValueError):
        value = default
    value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


def stable_shard_index(value: object, shard_count: int) -> int:
    count = max(1, int(shard_count or 1))
    digest = hashlib.sha256(str(value or "").strip().casefold().encode()).digest()
    return int.from_bytes(digest[:8], "big") % count


def account_shard_settings() -> tuple[int, int]:
    count = env_int("CHATGPT2API_ACCOUNT_SHARD_COUNT", 1, 1, 1024)
    index = env_int("CHATGPT2API_ACCOUNT_SHARD_INDEX", 0, 0, 1023)
    if index >= count:
        raise RuntimeError("account shard index must be smaller than count")
    return count, index
