"""Credential-free account selection fields retained in request log events.

Counts describe the scan for this request's shard/model/upload filters. A
successful early-exit scan is not a full-pool inventory.
"""

SELECTION_COUNT_FIELDS = (
    "account_shard_index", "account_shard_count", "selection_loop_count",
    "matched_count", "ready_count", "busy_count", "limited_count",
    "upload_limited_count", "available_slot_count",
)
SELECTION_FIELDS = (*SELECTION_COUNT_FIELDS, "account_wait_reason", "selection_wait_ms")
