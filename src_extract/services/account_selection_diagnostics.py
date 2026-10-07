"""Credential-free account selection fields retained in request log events.

Counts describe the scan for this request's model/upload filters, including
other shards when account_shard_fallback is 1. local_* counts stay shard-local. A
successful early-exit scan is not a full-pool inventory.
"""

SELECTION_COUNT_FIELDS = (
    "account_shard_index", "account_shard_count", "selection_loop_count",
    "matched_count", "ready_count", "busy_count", "limited_count",
    "upload_limited_count", "available_slot_count",
    "account_shard_fallback", "account_selected_shard_index",
    "local_matched_count", "local_ready_count", "local_busy_count",
)
SELECTION_FIELDS = (*SELECTION_COUNT_FIELDS, "account_wait_reason", "selection_wait_ms")
