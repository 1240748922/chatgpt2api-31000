"""Bounded, best-effort diagnostic logging after credentials are committed.

This queue never persists tokens or account state. Losing a diagnostic on an
abrupt exit/full queue must not roll back or fail a successful token exchange.
"""
import logging

from services.bounded_task_runner import BoundedTaskRunner
from utils.diagnostics import scrub_diagnostic_value

logger = logging.getLogger(__name__)
credential_log_runner = BoundedTaskRunner(
    name="credential-event-log", max_workers=1, queue_size=256,
    error_handler=lambda exc: logger.warning({
        "event": "credential_event_log_failed", "error_type": type(exc).__name__,
    }),
)


def defer_credential_log(write, kind: str, summary: str, detail: dict) -> None:
    sanitized = scrub_diagnostic_value(detail)
    if not credential_log_runner.submit(write, kind, summary, sanitized):
        # Bounded overload: no unbounded tasks and no synchronous DB fallback.
        logger.warning({"event": "credential_event_log_dropped", "reason": "queue_full_or_closed"})
