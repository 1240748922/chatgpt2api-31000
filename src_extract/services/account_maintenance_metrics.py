"""Request-local credential maintenance sub-timings; never retains credentials."""
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps


TOKEN_METRIC_LABELS = {
    "account_token_lookup_ms": "凭据读取与锁等待",
    "account_token_slot_ms": "刷新并发槽位等待",
    "account_token_http_ms": "刷新凭据请求",
    "account_token_save_ms": "凭据结果保存",
    "account_token_singleflight_ms": "等待同账号刷新",
}
_timings = ContextVar("image_token_timings", default=None)


def _add(metric, started):
    timings = _timings.get()
    if timings is not None:
        timings[metric] = timings.get(metric, 0.0) + max(0.0, (time.perf_counter() - started) * 1000)


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
