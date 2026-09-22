"""Bounded, non-queuing overlap of a signed image transfer and page warmup."""
import os
import threading
import time
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


def put_signed_image(session_kwargs, url, headers, data, timeout, deadline, timing):
    """Worker owns its Session; no account cookies/Authorization are copied."""
    timing["started"] = time.perf_counter()
    try:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ImageFailureError(failure=image_failure("task_interrupted"))
            timeout = min(timeout, remaining)
        with requests.Session(**session_kwargs) as session:
            response = session.put(url, headers=headers, data=data, timeout=timeout)
            ensure_ok(response, "image_upload", credential_scope="signed_asset")
    finally:
        timing["ended"] = time.perf_counter()
