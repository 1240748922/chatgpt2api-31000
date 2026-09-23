"""Request-local credential maintenance sub-timings; never retains credentials."""
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from services.maintenance_pressure import pressure_samples


TOKEN_METRIC_LABELS = {
    "account_token_lookup_ms": "凭据读取与锁等待",
    "account_token_slot_ms": "刷新并发槽位等待",
    "account_token_http_ms": "刷新凭据请求",
    "account_token_save_ms": "凭据结果保存",
    "account_token_write_wait_ms": "凭据写入锁等待",
    "account_token_writer_lock_ms": "账号写入串行锁等待",
    "account_token_dispatch_lock_ms": "账号状态锁等待",
    "account_token_commit_ms": "凭据数据库提交",
    "account_token_conflict_ms": "凭据冲突重读",
    "account_token_log_ms": "凭据日志提交",
    "account_token_singleflight_ms": "等待同账号刷新",
}
_timings = ContextVar("image_token_timings", default=None)


def _add(metric, started):
    elapsed = max(0.0, (time.perf_counter() - started) * 1000)
    if metric == "account_token_writer_lock_ms":
        pressure_samples.observe("writer_wait_ms", elapsed)
    elif metric == "account_token_commit_ms":
        pressure_samples.observe("database_ms", elapsed)
    timings = _timings.get()
    if timings is not None:
        timings[metric] = timings.get(metric, 0.0) + elapsed


@contextmanager
def token_phase(metric):
    started = time.perf_counter()
    try:
        yield
    finally:
        _add(metric, started)


@contextmanager
def token_request_slot(factory):
    started = time.perf_counter()
    entered = False
    try:
        with factory():
            entered = True
            _add("account_token_slot_ms", started)
            yield
    finally:
        if not entered:
            _add("account_token_slot_ms", started)


@contextmanager
def token_write_lock(lock, metric=None):
    with token_phase("account_token_write_wait_ms"):
        if metric:
            with token_phase(metric):
                lock.acquire()
        else:
            lock.acquire()
    try:
        yield
    finally:
        lock.release()


def collect_token_timings(function):
    @wraps(function)
    def collect(self, *args, **kwargs):
        timings = {}
        token = _timings.set(timings)
        try:
            return function(self, *args, **kwargs)
        finally:
            _timings.reset(token)
            setter = getattr(self, "_set_image_selection_diagnostics", None)
            if callable(setter):
                # Diagnostics are merged, including when selection fails before
                # resetting them. Clear untouched phases from the previous call.
                setter(**{key: int(timings.get(key, 0)) for key in TOKEN_METRIC_LABELS})
    return collect
