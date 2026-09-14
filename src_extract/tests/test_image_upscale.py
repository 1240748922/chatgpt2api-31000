from __future__ import annotations

import hashlib
from pathlib import Path

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
