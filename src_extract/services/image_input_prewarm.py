"""Bounded overlap of image transfer/confirmation and page warmup."""
import os
import threading
import time
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests

from services.image_failure import ImageFailureError, image_failure
from utils.helper import ensure_ok


INPUT_METRIC_LABELS = {
    "input_prepare_ms": "上传与预热总耗时",
    "upload_decode_ms": "输入图读取与解码",
    "upload_register_ms": "申请上传地址",
    "upload_put_ms": "传输输入图",
    "upload_confirm_ms": "确认上传",
    "prewarm_overlap_ms": "上传与预热重叠",
}


class NonQueuingPool:
    def __init__(self, capacity):
        self._slots = threading.BoundedSemaphore(capacity)
        self._executor = ThreadPoolExecutor(max_workers=max(1, capacity), thread_name_prefix="image-input")

    def try_submit(self, function, *args):
        if not self._slots.acquire(blocking=False):
            return None
        try:
            future = self._executor.submit(function, *args)
        except RuntimeError:
            self._slots.release()
            return None  # Shutdown: the caller retains the serial path.
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _: self._slots.release())
        return future


def _capacity():
    try:
        return max(0, min(1024, int(os.getenv("CHATGPT2API_IMAGE_PREWARM_CONCURRENCY", "64"))))
    except ValueError:
        return 64


input_transfer_pool = NonQueuingPool(_capacity())


def snapshot_cookies(cookies):
    return {(cookie.domain, cookie.path, cookie.name): deepcopy(cookie) for cookie in cookies.jar}


def merge_upload_cookies(cookies, baseline, uploaded):
    """Three-way merge: never overwrite cookies changed by concurrent warmup."""
    def values(cookie):
        return vars(cookie) if cookie is not None else None

    current = snapshot_cookies(cookies)
    for key in baseline.keys() | uploaded.keys():
        before, after, live = baseline.get(key), uploaded.get(key), current.get(key)
        if values(before) == values(after) or values(live) != values(before):
            continue
        if after is None:
            if live is not None:
                cookies.jar.clear(*key)
        else:
            cookies.jar.set_cookie(deepcopy(after))


def put_signed_image(session_kwargs, url, headers, data, timeout, deadline, timing, confirmation=None):
    """Own Session: asset PUT has no credentials; optional confirm is upstream-only."""
    def remaining(budget):
        if deadline is None:
            return budget
        left = deadline - time.monotonic()
        if left <= 0:
            raise ImageFailureError(failure=image_failure("task_interrupted"))
        return min(budget, left)

    timing["started"] = time.perf_counter()
    try:
        timeout = remaining(timeout)
        with requests.Session(**session_kwargs) as session:
            try:
                response = session.put(url, headers=headers, data=data, timeout=remaining(timeout))
                ensure_ok(response, "image_upload", credential_scope="signed_asset")
            finally:
                timing["put_ended"] = time.perf_counter()
            if confirmation is not None:
                timing["confirm_started"] = time.perf_counter()
                try:
                    # Do not install account cookies/default headers until the
                    # signed-asset transfer is over. Confirmation uses the same
                    # upstream identity and proxy, but never the main Session.
                    session.cookies.clear()
                    for cookie in confirmation["cookies"].values():
                        session.cookies.jar.set_cookie(deepcopy(cookie))
                    session.headers.update(confirmation["session_headers"])
                    response = session.post(
                        confirmation["url"], headers=confirmation["headers"], data="{}",
                        timeout=remaining(60),
                    )
                    ensure_ok(response, confirmation["path"])
                    return snapshot_cookies(session.cookies)
                finally:
                    timing["confirm_ended"] = time.perf_counter()
    finally:
        timing["ended"] = time.perf_counter()
        timing.setdefault("put_ended", timing["ended"])
