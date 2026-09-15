from __future__ import annotations

import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from queue import LifoQueue
from threading import Barrier
from types import SimpleNamespace

import pytest

from services import image_upscale_service as upscale


def test_fsrcnn_model_is_pinned_and_present() -> None:
    model = Path(upscale._FSRCNN_MODEL)

    assert model.is_file()
    assert hashlib.sha256(model.read_bytes()).hexdigest() == (
        "366b33f0084c7b3f2bf6724f0a2c77bca94fcec9d7b6d72389d330073b380d5c"
    )


def test_fsrcnn_missing_runtime_returns_original(monkeypatch) -> None:
    original = b"encoded-image"
    monkeypatch.setattr(upscale, "_FSRCNN_MODEL", Path("missing-fsrcnn-model.pb"))
    monkeypatch.setitem(upscale.config.data, "image_upscale_enabled", True)

    # The public helper treats an unavailable engine as a recoverable image
    # processing failure, so an upstream image is still returned intact.
    monkeypatch.setitem(upscale.config.data, "image_upscale_engine", "fsrcnn_x2")
    monkeypatch.setattr(upscale, "image_size_from_bytes", lambda _: (64, 64))
    assert upscale.upscale_image_if_needed(original, "128x128") == original


def test_fsrcnn_inference_leases_are_parallel_and_returned_on_error(monkeypatch):
    models = LifoQueue(maxsize=2)
    first, second = object(), object()
    models.put(first)
    models.put(second)
    monkeypatch.setattr(upscale, "_FSRCNN_MODELS", models)
    barrier = Barrier(2, timeout=2)
    def infer():
        with upscale._lease_fsrcnn_model(SimpleNamespace()) as model:
            barrier.wait()
            return model
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(infer) for _ in range(2)]
        assert {f.result() for f in futures} == {first, second}
    assert models.qsize() == 2
    with pytest.raises(RuntimeError):
        with upscale._lease_fsrcnn_model(SimpleNamespace()):
            raise RuntimeError("inference failed")
    assert models.qsize() == 2
