"""Bounded overlap of image transfer/confirmation and page warmup."""
import os
import threading
import time
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

from curl_cffi import requests
from PIL import Image

from services.image_failure import ImageFailureError, image_failure
from utils.helper import ensure_ok


INPUT_METRIC_LABELS = {
    "page_prewarm_age_ms": "请求前预热库存年龄",
    "input_prepare_ms": "上传与预热总耗时",
    "upload_decode_ms": "输入图读取与解码",
    "upload_register_ms": "申请上传地址",
    "upload_put_ms": "传输输入图",
    "upload_confirm_ms": "确认上传",
    "prewarm_overlap_ms": "上传与预热重叠",
    "bootstrap_first_ms": "页面预热首次请求",
    "bootstrap_retry_ms": "页面预热重连请求",
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


def prepare_image_upload(image, file_name, decode_image):
    """Read one unchanged input at a time; do not materialize a whole batch."""
    data = decode_image(image)
    if image and len(image) < 512 and not image.startswith("data:") and "\n" not in image and "\r" not in image:
        path = Path(os.path.expanduser(image))
        if path.is_file():
            file_name = path.name
    with Image.open(BytesIO(data)) as decoded:
        width, height = decoded.size
        mime_type = Image.MIME.get(decoded.format, "image/png")
    return data, {"file_name": file_name, "file_size": len(data), "mime_type": mime_type,
                  "width": width, "height": height}


class _UploadCancelled(Exception):
    """Warmup failed: finish in-flight I/O but do not start another upload step."""


@contextmanager
def _upload_step(timing, metric):
    started = time.perf_counter()
    try:
        yield
    except Exception as exc:
        if not getattr(exc, "failure_phase", ""):
            exc.failure_phase = "uploading"
            exc.failure_phase_ms = max(0, int((time.perf_counter() - started) * 1000))
        raise
    finally:
        metrics = timing.setdefault("tail_metrics", {})
        metrics[metric] = metrics.get(metric, 0) + (time.perf_counter() - started) * 1000


def _upload_tail(session, session_kwargs, confirmation, put_headers, images, decode_image, remaining, timing):
    """One worker uploads remaining references serially; only page warmup overlaps."""
    remaining(60)
    base = confirmation["url"].split("/backend-api/files/", 1)[0]

    def headers(path):
        return {**confirmation["headers"], "X-OpenAI-Target-Path": path, "X-OpenAI-Target-Route": path}

    # The authenticated worker Session is never used for another signed PUT.
    # Reuse a separate credential-free asset connection for the whole tail.
    with requests.Session(**session_kwargs) as asset_session:
        for image, file_name in images:
            remaining(60)
            with _upload_step(timing, "upload_decode_ms"):
                data, reference = prepare_image_upload(image, file_name, decode_image)
            path = "/backend-api/files"
            with _upload_step(timing, "upload_register_ms"):
                response = session.post(base + path, headers=headers(path), json={
                    "file_name": reference["file_name"], "file_size": reference["file_size"],
                    "use_case": "multimodal", "width": reference["width"], "height": reference["height"],
                }, timeout=remaining(60))
                ensure_ok(response, path)
                meta = response.json()
            with _upload_step(timing, "upload_put_ms"):
                asset_session.cookies.clear()
                response = asset_session.put(meta["upload_url"],
                    headers={**put_headers, "Content-Type": reference["mime_type"]},
                    data=data, timeout=remaining(120))
                ensure_ok(response, "image_upload", credential_scope="signed_asset")
            path = f"/backend-api/files/{meta['file_id']}/uploaded"
            with _upload_step(timing, "upload_confirm_ms"):
                response = session.post(base + path, headers=headers(path), data="{}", timeout=remaining(60))
                ensure_ok(response, path)
            timing.setdefault("references", []).append({"file_id": meta["file_id"], **reference})


def put_signed_image(session_kwargs, url, headers, data, timeout, deadline, timing, confirmation=None,
                     remaining_images=(), decode_image=None, stop_event=None):
    """Own Sessions: ordered uploads overlap warmup without sharing credentials with assets."""
    def remaining(budget):
        if stop_event is not None and stop_event.is_set():
            raise _UploadCancelled()
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
                finally:
                    timing["confirm_ended"] = time.perf_counter()
                if remaining_images:
                    _upload_tail(session, session_kwargs, confirmation, headers, remaining_images,
                                 decode_image, remaining, timing)
                return snapshot_cookies(session.cookies)
    except _UploadCancelled:
        # The caller still raises the original warmup error after draining us.
        return None
    finally:
        timing["ended"] = time.perf_counter()
        timing.setdefault("put_ended", timing["ended"])
