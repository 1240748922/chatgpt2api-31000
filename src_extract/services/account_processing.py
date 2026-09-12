"""Shared concurrency control for remote and local account batch work."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Executor, Future, wait
from contextlib import AbstractContextManager, contextmanager
from functools import wraps
from threading import Condition, Lock, local
from typing import Callable, Iterable, Iterator, ParamSpec, TypeVar


_DEFAULT_CONCURRENCY = 30
_DEFAULT_IMPORT_CONCURRENCY = 30
_DEFAULT_QUOTA_SYNC_CONCURRENCY = 20
_MIN_CONCURRENCY = 1
_MAX_CONCURRENCY = 100

P = ParamSpec("P")
R = TypeVar("R")


def account_processing_concurrency() -> int:
    """Return the current configured global account-processing limit."""
    try:
        from services.config import config

        value = int(config.account_processing_concurrency)
    except (AttributeError, TypeError, ValueError):
        value = _DEFAULT_CONCURRENCY
    return max(_MIN_CONCURRENCY, min(_MAX_CONCURRENCY, value))


def account_processing_worker_count(item_count: int) -> int:
    """Size a local worker pool without exceeding the shared global limit."""
    count = max(0, int(item_count or 0))
    return min(count, account_processing_concurrency())


def _configured_concurrency(name: str, default: int) -> int:
    try:
        from services.config import config

        value = int(getattr(config, name))
    except (AttributeError, TypeError, ValueError):
        value = default
    return max(_MIN_CONCURRENCY, min(_MAX_CONCURRENCY, value))


def account_import_concurrency() -> int:
    """Return the independent remote-account import limit."""
    return _configured_concurrency(
        "account_import_concurrency",
        _DEFAULT_IMPORT_CONCURRENCY,
    )


def account_import_worker_count(item_count: int) -> int:
    return min(max(0, int(item_count or 0)), account_import_concurrency())


def account_quota_sync_concurrency() -> int:
    """Return the independent account metadata/quota sync limit."""
    return _configured_concurrency(
        "account_quota_sync_concurrency",
        _DEFAULT_QUOTA_SYNC_CONCURRENCY,
    )


def account_quota_sync_worker_count(item_count: int) -> int:
    return min(max(0, int(item_count or 0)), account_quota_sync_concurrency())


def bounded_future_results(
    executor: Executor,
    function: Callable[[object], R],
    items: Iterable[object],
    *,
    max_in_flight: int,
) -> Iterator[tuple[Future[R], object]]:
    """Submit a bounded window so large account batches do not create 30k futures."""
    iterator = iter(items)
    pending: dict[Future[R], object] = {}
    window = max(1, int(max_in_flight))

    def fill_window() -> None:
        while len(pending) < window:
            try:
                item = next(iterator)
            except StopIteration:
                return
            pending[executor.submit(function, item)] = item

    fill_window()
    while pending:
        completed, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
        for future in completed:
            item = pending.pop(future)
            yield future, item
            fill_window()


class AccountProcessingLimiter:
    """A process-wide, dynamically sized and thread-reentrant limiter."""

    def __init__(self, limit_provider: Callable[[], int] = account_processing_concurrency) -> None:
        self._condition = Condition(Lock())
        self._active = 0
        self._waiting = 0
        self._local = local()
        self._limit_provider = limit_provider

    @contextmanager
    def slot(self) -> Iterator[None]:
        depth = int(getattr(self._local, "depth", 0) or 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth = depth
            return

        with self._condition:
            self._waiting += 1
            try:
                while self._active >= self._limit_provider():
                    self._condition.wait(timeout=0.5)
                self._active += 1
            finally:
                self._waiting = max(0, self._waiting - 1)
            self._local.depth = 1
        try:
            yield
        finally:
            self._local.depth = 0
            with self._condition:
                self._active = max(0, self._active - 1)
                self._condition.notify_all()

    def snapshot(self) -> dict[str, int]:
        with self._condition:
            return {
                "limit": self._limit_provider(),
                "active": self._active,
                "waiting": self._waiting,
            }

    @contextmanager
    def batch_slot(self) -> Iterator[None]:
        """Reserve one shared slot for the lifetime of an account batch.

        Batch work and individual upstream requests share the same limiter and
        thread-local re-entry depth.  A batch may therefore call code that
        acquires a regular slot on its coordinator thread without consuming a
        second slot or deadlocking when the configured limit is one.
        """
        with self.slot():
            yield


account_processing_limiter = AccountProcessingLimiter()
account_import_limiter = AccountProcessingLimiter(account_import_concurrency)
account_quota_sync_limiter = AccountProcessingLimiter(account_quota_sync_concurrency)


def account_processing_slot() -> AbstractContextManager[None]:
    """Acquire one shared slot for an upstream account-maintenance request."""
    return account_processing_limiter.slot()


def account_processing_batch_slot() -> AbstractContextManager[None]:
    """Acquire one shared slot for a complete local account batch mutation.

    The slot is intentionally the same limiter used by remote maintenance. A
    local mutation stays atomic inside the account service, but still participates
    in the one process-wide capacity budget.
    """
    return account_processing_limiter.batch_slot()


def account_import_slot() -> AbstractContextManager[None]:
    """Acquire capacity dedicated to remote account import requests."""
    return account_import_limiter.slot()


def account_quota_sync_slot() -> AbstractContextManager[None]:
    """Acquire capacity dedicated to remote account/quota checks."""
    return account_quota_sync_limiter.slot()


def account_processing_batch(
    function: Callable[P, R],
) -> Callable[P, R]:
    """Decorate a synchronous account batch entry point with one shared slot."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with account_processing_batch_slot():
            return function(*args, **kwargs)

    return wrapped
