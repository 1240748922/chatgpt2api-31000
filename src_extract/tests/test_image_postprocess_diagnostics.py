from __future__ import annotations

import base64
from types import SimpleNamespace

from services.protocol import conversation
from services.realtime_monitor_service import RealtimeMonitorService
from services.request_detail_view import build_request_timeline_presentation


def test_image_postprocess_metrics_include_upscale_and_storage(monkeypatch):
    monitor = RealtimeMonitorService()
    monitor.start("postprocess-test", endpoint="/v1/images/generations", model="gpt-image-2")
    monkeypatch.setattr(conversation, "realtime_monitor_service", monitor)
    request = SimpleNamespace(
        trace_image_perf=True,
        call_id="postprocess-test",
        model="gpt-image-2",
        monitor_attempt=1,
    )
    monkeypatch.setattr(conversation, "upscale_image_if_needed", lambda data, size: data + b"-upscaled")
    monkeypatch.setattr(conversation, "save_image_bytes", lambda data, base_url, deadline_monotonic=None: "https://example.test/image.png")

    result = conversation.format_image_result(
        [{"b64_json": base64.b64encode(b"image").decode("ascii")}],
        "prompt",
        "url",
        monitor_request=request,
        index=1,
        total=1,
    )

    metrics = result["_image_processing_metrics"]
    assert metrics["upscale_ms"] >= 0
    assert metrics["storage_ms"] >= 0
    assert metrics["postprocess_ms"] >= metrics["upscale_ms"]
    detail = monitor.call_detail("postprocess-test")
    assert detail is not None
    assert detail["metrics"]["upscale_ms"] >= 0
    assert detail["metrics"]["storage_ms"] >= 0
    assert detail["metrics"]["postprocess_ms"] >= 0


def test_collect_image_outputs_keeps_postprocess_metrics():
    output = conversation.ImageOutput(
        kind="result",
        model="gpt-image-2",
        index=1,
        total=1,
        data=[{"b64_json": base64.b64encode(b"image").decode("ascii")}],
        processing_metrics={
            "upscale_ms": 12,
            "storage_ms": 34,
            "postprocess_ms": 46,
        },
    )
    result = conversation.collect_image_outputs([output])
    assert result["_image_processing_metrics"] == {
        "upscale_ms": 12,
        "storage_ms": 34,
        "postprocess_ms": 46,
    }


def test_request_detail_timeline_includes_postprocess_steps():
    timeline = build_request_timeline_presentation(
        {
            "upscale_ms": 1200,
            "storage_ms": 300,
            "postprocess_ms": 1500,
        },
        [],
        image_count=1,
    )

    group = next(item for item in timeline["groups"] if item["key"] == "postprocess")
    assert [item["key"] for item in group["steps"]] == [
        "upscale_ms",
        "storage_ms",
        "postprocess_ms",
    ]
