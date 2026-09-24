"""Bounded, in-memory telemetry for the lifecycle watcher, never credential data.

The watcher context is captured before submitting workers; executor threads do
not inherit ContextVars. Manual sync/imports deliberately have no batch context.
No account/DB locks or network calls are used by this module.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from threading import Lock

_current_batch = ContextVar("account_maintenance_batch", default=None)
OUTCOMES = ("succeeded", "failed", "skipped")


def current_maintenance_batch():
    return _current_batch.get()


class MaintenanceBatch:
    def __init__(self, owner, kind, total):
        self.owner = owner
        self.data = dict(kind=kind, total=total, active=0, completed=0,
                         succeeded=0, failed=0, skipped=0, status="running",
                         started_at=time.time(), finished_at=None)

    def call(self, operation, classify):
        with self.owner._lock:
            self.data["active"] += 1
            self.owner._active += 1
        outcome = "failed"
        try:
            result = operation()
            outcome = classify(result)
            return result
        finally:
            with self.owner._lock:
                self.data["active"] -= 1
                self.owner._active -= 1
                self._complete_locked(outcome if outcome in OUTCOMES else "failed", 1)

    def _complete_locked(self, outcome, count):
        self.data[outcome] += count
        self.data["completed"] += count
        self.owner._totals[outcome] += count
        self.owner._totals["completed"] += count

    def reconcile(self, result):
        # Preflight failures (e.g. deleted account) can finish before submission.
        # Merge only aggregate counts; never retain result rows or raw errors.
        expected = {"succeeded": result.get("refreshed", result.get("synced", 0)),
                    "failed": len(result.get("errors") or []), "skipped": result.get("skipped", 0)}
        with self.owner._lock:
            for outcome, count in expected.items():
                if type(count) is int and count >= 0:
                    missing = min(max(0, count - self.data[outcome]),
                                  max(0, self.data["total"] - self.data["completed"] - self.data["active"]))
                    self._complete_locked(outcome, missing)


class AccountMaintenanceProgress:
    def __init__(self):
        self._lock = Lock()
        self._state = "not_started"
        self._started_at = None
        self._next_check_at = None
        self._active = 0
        self._current = None
        self._last = None
        self._totals = dict(completed=0, succeeded=0, failed=0, skipped=0)

    def started(self):
        with self._lock:
            self._started_at = self._started_at or time.time()
            self._state = "checking"
            self._next_check_at = None

    def checking(self):
        with self._lock:
            self._state = "checking"
            self._next_check_at = None

    def waiting(self, seconds):
        with self._lock:
            self._state = "waiting"
            self._next_check_at = time.time() + max(0, seconds)

    def stopped(self):
        with self._lock:
            self._state = "stopped"
            self._next_check_at = None

    @contextmanager
    def batch(self, kind, total):
        batch = MaintenanceBatch(self, kind, total)
        with self._lock:
            self._current = batch
        context = _current_batch.set(batch)
        status = "interrupted"
        try:
            yield batch
            status = "completed"
        finally:
            _current_batch.reset(context)
            with self._lock:
                batch.data["status"] = status
                batch.data["finished_at"] = time.time()
                self._last = batch
                if self._current is batch:
                    self._current = None

    def snapshot(self, *, owner=True, instance="app0"):
        with self._lock:
            known = owner and self._started_at is not None
            batch = deepcopy(self._current.data) if known and self._current else None
            if batch:
                batch["queued"] = max(0, batch["total"] - batch["active"] - batch["completed"])
            return {
                "scope": "lifecycle_renewal_and_quota", "instance": instance,
                "owner": owner, "available": known, "sampled_at": time.time(),
                "started_at": self._started_at if known else None,
                "state": self._state if owner else "not_owner",
                "active": self._active if known else None,
                "batch": batch,
                "last_batch": deepcopy(self._last.data) if known and self._last else None,
                "totals": deepcopy(self._totals) if known else None,
                "next_check_at": self._next_check_at if known else None,
            }


account_maintenance_progress = AccountMaintenanceProgress()
