from __future__ import annotations

import io
from contextlib import contextmanager
from queue import LifoQueue, Empty, Full
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from PIL import Image, ImageOps

from services.config import config
from services.runtime_configuration import env_int
from utils.diagnostics import diagnostic_excerpt
from utils.image_tokens import image_size_from_bytes
from utils.log import logger


_SHARP_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "image_upscale" / "upscale.mjs"
_FSRCNN_MODEL = Path(__file__).resolve().parents[1] / "models" / "FSRCNN_x2.pb"
# Keep post-processing from becoming the bottleneck for image traffic. This is
# per API replica; the deployment can lower it through the environment when
# CPU saturation is observed.
_UPSCALE_CONCURRENCY = env_int("CHATGPT2API_IMAGE_UPSCALE_CONCURRENCY", 64, 1, 512)
_FSRCNN_CONCURRENCY = env_int("CHATGPT2API_FSRCNN_CONCURRENCY", 64, 1, 512)
_FSRCNN_MODELS = LifoQueue(maxsize=_FSRCNN_CONCURRENCY)
_FSRCNN_INIT_LOCK = threading.Lock()
_FSRCNN_RUNTIME_READY = False
_UPSCALE_STATE_LOCK = threading.Lock()
_UPSCALE_ACTIVE = 0
_UPSCALE_WAITING = 0
_MAX_TARGET_DIMENSION = 8192


def image_upscale_snapshot() -> dict[str, int]:
    limit = config.image_upscale_concurrency
    with _UPSCALE_STATE_LOCK:
        return {"limit": limit, "active": _UPSCALE_ACTIVE, "waiting": _UPSCALE_WAITING}


class _UpscaleSlot:
    """Dynamic per-replica gate controlled by the system settings page."""

    def __init__(self) -> None:
        self._started = 0.0
        self._acquired = False

    def __enter__(self) -> int:
        global _UPSCALE_ACTIVE, _UPSCALE_WAITING
        self._started = time.perf_counter()
        with _UPSCALE_STATE_LOCK:
            _UPSCALE_WAITING += 1
        try:
            while True:
                limit = config.image_upscale_concurrency
                with _UPSCALE_STATE_LOCK:
                    if _UPSCALE_ACTIVE < limit:
                        _UPSCALE_WAITING -= 1
                        _UPSCALE_ACTIVE += 1
                        self._acquired = True
                        break
                time.sleep(0.05)
        except BaseException:
            with _UPSCALE_STATE_LOCK:
                if not self._acquired:
                    _UPSCALE_WAITING -= 1
            raise
        return int((time.perf_counter() - self._started) * 1000)

    def __exit__(self, exc_type, exc, traceback) -> None:
        global _UPSCALE_ACTIVE
        if not self._acquired:
            return
        with _UPSCALE_STATE_LOCK:
            _UPSCALE_ACTIVE -= 1


def _target_size(value: object) -> tuple[int, int] | None:
    text = str(value or "").strip().lower()
    if not text or text == "auto":
        return None
    match = re.fullmatch(r"(\d{2,5})\s*x\s*(\d{2,5})", text)
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if width > _MAX_TARGET_DIMENSION or height > _MAX_TARGET_DIMENSION:
        return None
    return width, height


def _pillow_lanczos(image_data: bytes, target: tuple[int, int]) -> bytes:
    with Image.open(io.BytesIO(image_data)) as source:
        image_format = str(source.format or "PNG").upper()
        image = ImageOps.exif_transpose(source)
        resized = image.resize(target, Image.Resampling.LANCZOS, reducing_gap=3.0)
        output = io.BytesIO()
        save_options: dict[str, object] = {}
        if image_format in {"JPG", "JPEG"}:
            image_format = "JPEG"
            if resized.mode not in {"RGB", "L"}:
                resized = resized.convert("RGB")
            save_options.update(quality=95, subsampling=0)
        elif image_format == "WEBP":
            save_options.update(quality=95, method=4)
        elif image_format not in {"PNG", "WEBP"}:
            image_format = "PNG"
        resized.save(output, format=image_format, **save_options)
        return output.getvalue()


def _sharp_lanczos3(image_data: bytes, target: tuple[int, int]) -> bytes:
    node = shutil.which("node")
    if not node or not _SHARP_SCRIPT.is_file():
        raise RuntimeError("Sharp runtime is unavailable")
    completed = subprocess.run(
        [node, str(_SHARP_SCRIPT), str(target[0]), str(target[1])],
        input=image_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=180,
    )
    if completed.returncode != 0 or not completed.stdout:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"Sharp exited with code {completed.returncode}")
    return completed.stdout


@contextmanager
def _lease_fsrcnn_model(cv2):
    try:
        model = _FSRCNN_MODELS.get_nowait()
    except Empty:
        if not hasattr(cv2, "dnn_superres"):
            raise RuntimeError("OpenCV dnn_superres is unavailable")
        model = cv2.dnn_superres.DnnSuperResImpl_create()
        model.readModel(str(_FSRCNN_MODEL))
        model.setModel("fsrcnn", 2)
    try:
        yield model
    finally:
        try:
            _FSRCNN_MODELS.put_nowait(model)
        except Full:
            pass


def _fsrcnn_x2(image_data: bytes, target: tuple[int, int]) -> bytes:
    """Run the small CPU FSRCNN x2 model, then fit the exact requested size."""

    if not _FSRCNN_MODEL.is_file():
        raise RuntimeError("FSRCNN model is unavailable")
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("OpenCV contrib runtime is unavailable") from exc

    encoded = np.frombuffer(image_data, dtype=np.uint8)
    source = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
    if source is None:
        raise RuntimeError("FSRCNN could not decode image")

    source_is_gray = len(source.shape) == 2
    source_has_alpha = len(source.shape) == 3 and source.shape[2] == 4
    if source_is_gray:
        model_input = cv2.cvtColor(source, cv2.COLOR_GRAY2BGR)
    elif source_has_alpha:
        model_input = source[:, :, :3]
    else:
        model_input = source

    global _FSRCNN_RUNTIME_READY
    with _FSRCNN_INIT_LOCK:
        if not _FSRCNN_RUNTIME_READY:
            # Eight API replicas must not each let OpenCV claim every CPU core.
            cv2.setNumThreads(env_int("CHATGPT2API_FSRCNN_THREADS", 1, 1, 16))
            _FSRCNN_RUNTIME_READY = True
    # Each admitted request leases an independent model; no lock covers inference.
    with _lease_fsrcnn_model(cv2) as model:
        upscaled = model.upsample(model_input)
    if source_has_alpha:
        alpha = cv2.resize(source[:, :, 3], (upscaled.shape[1], upscaled.shape[0]), interpolation=cv2.INTER_CUBIC)
        upscaled = cv2.merge((*cv2.split(upscaled), alpha))
    elif source_is_gray:
        upscaled = cv2.cvtColor(upscaled, cv2.COLOR_BGR2GRAY)

    if (upscaled.shape[1], upscaled.shape[0]) != target:
        upscaled = cv2.resize(upscaled, target, interpolation=cv2.INTER_LANCZOS4)
    with Image.open(io.BytesIO(image_data)) as source_image:
        image_format = str(source_image.format or "PNG").upper()
    extension = ".jpg" if image_format in {"JPG", "JPEG"} else ".webp" if image_format == "WEBP" else ".png"
    success, output = cv2.imencode(extension, upscaled)
    if not success:
        raise RuntimeError("FSRCNN could not encode image")
    return output.tobytes()


def upscale_image_if_needed(
    image_data: bytes, requested_size: object, *, timings: dict[str, int] | None = None,
) -> bytes:
    if timings is not None:
        timings.update(upscale_queue_ms=0, upscale_exec_ms=0)
    if not image_data or not config.image_upscale_enabled:
        return image_data
    target = _target_size(requested_size)
    source = image_size_from_bytes(image_data)
    if not target or not source:
        return image_data
    if source[0] >= target[0] and source[1] >= target[1]:
        return image_data

    engine = config.image_upscale_engine
    with _UpscaleSlot() as queue_ms:
        if timings is not None:
            timings["upscale_queue_ms"] = queue_ms
        upscale_started = time.perf_counter()
        try:
            if engine == "sharp_lanczos3":
                try:
                    result = _sharp_lanczos3(image_data, target)
                except Exception as exc:
                    logger.warning({
                        "event": "image_upscale_sharp_fallback",
                        "source_size": list(source),
                        "target_size": list(target),
                        "error": diagnostic_excerpt(exc, 500),
                    })
                    result = _pillow_lanczos(image_data, target)
                    engine = "pillow_lanczos"
            elif engine == "fsrcnn_x2":
                result = _fsrcnn_x2(image_data, target)
            else:
                result = _pillow_lanczos(image_data, target)
            logger.info({
                "event": "image_upscale_done",
                "engine": engine,
                "source_size": list(source),
                "target_size": list(target),
                "source_bytes": len(image_data),
                "result_bytes": len(result),
                "queue_ms": queue_ms,
                "upscale_ms": int((time.perf_counter() - upscale_started) * 1000),
            })
            return result
        except Exception as exc:
            logger.warning({
                "event": "image_upscale_failed",
                "engine": engine,
                "source_size": list(source),
                "target_size": list(target),
                "queue_ms": queue_ms,
                "upscale_ms": int((time.perf_counter() - upscale_started) * 1000),
                "error": diagnostic_excerpt(exc, 500),
            })
            return image_data
        finally:
            if timings is not None:
                timings["upscale_exec_ms"] = max(0, int((time.perf_counter() - upscale_started) * 1000))
