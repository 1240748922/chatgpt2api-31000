import base64
from collections import deque
from copy import deepcopy
from types import SimpleNamespace

import pytest

from services.protocol import conversation
from services.realtime_monitor_service import RealtimeMonitorService
from services.image_failure import ImageDownloadError, image_failure
from services.image_postprocess_metrics import POSTPROCESS_METRIC_LABELS
from services.monitor_view import _project_event
from api.monitor_contract import MonitorEventView
from test_image_recovery import Clock
from test_log_detail_api import log_api


@pytest.mark.parametrize("fail", [False, True])
def test_actual_postprocess_to_attempt_to_persisted_api(log_api, monkeypatch, fail):
    logs, client = log_api
    clock = Clock()
    monitor = RealtimeMonitorService()
    monkeypatch.setattr(conversation, "time", clock)
    monkeypatch.setattr(conversation, "realtime_monitor_service", monitor)
    monitor.start("processed", endpoint="/v1/images/edits", model="gpt-image-2")
    monitor.stage("processed", "image_getting_account", index=1, attempt=1)
    monitor.stage("processed", "image_stream_resolve_start", index=1, attempt=1, sse_stream_ms=31000)
    request = SimpleNamespace(call_id="processed", model="fixture", trace_image_perf=True, monitor_attempt=1)
    def upscale(data, size, *, timings):
        clock.sleep(2.5)
        timings.update(upscale_queue_ms=100, upscale_exec_ms=2400)
        return data
    def save(*args, timings, **kwargs):
        clock.sleep(60)
        timings.update(storage_catalog_ms=60000)
        if fail:
            raise ImageDownloadError("fixture catalog failed", failure=image_failure("image_storage_failed"))
        return "http://example.test/images/test.png"
    monkeypatch.setattr(conversation, "upscale_image_if_needed", upscale)
    monkeypatch.setattr(conversation, "save_image_bytes", save)
    def process():
        return conversation.format_image_result(
            [{"b64_json": base64.b64encode(b"image").decode()}], "fixture", "url",
            monitor_request=request, index=1, total=1,
        )
    if fail:
        with pytest.raises(ImageDownloadError):
            process()
    else:
        process()
    # Simulate other requests evicting ALL this call's global monitor events.
    monitor._events = deque(maxlen=3)
    for i in range(6):
        monitor.start(f"other-{i}", endpoint="/fixture", model="fixture")
    detail = {"call_id": "processed", "status": "failed" if fail else "success",
              "endpoint": "/v1/images/edits", "duration_ms": 93500,
              "image_attempts": [{"slot": 1, "attempt": 1, "status": "failed" if fail else "success",
                                  "duration_ms": 93500}]}
    monitor.finish(detail)
    logs.append_item({"id": "processed", "type": "call", "detail": detail})
    response = client.get("/api/logs/processed")
    assert response.status_code == 200, response.text
    attempt = response.json()["attempts"][0]
    assert attempt["timings_ms"]["upscale_ms"] == 2500
    assert attempt["timings_ms"]["upscale_queue_ms"] == 100
    assert attempt["timings_ms"]["storage_ms"] == 60000
    assert attempt["timings_ms"]["postprocess_ms"] == 62500
    timeline = attempt["presentation"]["timeline"]
    assert sum(s["value_ms"] for s in timeline["segments"]) == 93500
    assert any(g["key"] == "postprocess" for g in timeline["groups"])


def test_every_postprocess_metric_survives_event_projection_and_contract():
    monitor = RealtimeMonitorService()
    monitor.start("events", endpoint="/v1/images/edits", model="fixture")
    metrics = {key: 1234 for key in POSTPROCESS_METRIC_LABELS}
    monitor.stage("events", "image_postprocess_done", index=1, attempt=1, **metrics)
    event = monitor._events[-1]
    for key in metrics:
        assert event[key] == 1234
    projected = _project_event(event)
    parsed = MonitorEventView.model_validate(projected)
    for key in metrics:
        assert getattr(parsed, key) == 1234


def test_save_retry_reuses_downloaded_bytes_and_accumulates_cost(monkeypatch):
    payloads = []
    def save(data, base_url, *, timings, **kwargs):
        payloads.append(data)
        timings["storage_catalog_ms"] = 12
        if len(payloads) == 1:
            raise OSError("fixture transient storage error")
        return SimpleNamespace(url="http://example.test/images/ok.png")
    monkeypatch.setattr(conversation, "image_storage_service", SimpleNamespace(save=save))
    timings = {}
    assert conversation.save_image_bytes(b"already-generated", timings=timings).endswith("/ok.png")
    assert payloads == [b"already-generated", b"already-generated"]
    assert timings["storage_catalog_ms"] == 24


def test_successful_image_does_not_keep_previous_retry_failure():
    monitor = RealtimeMonitorService()
    monitor.start("recovered", endpoint="/v1/images/edits", model="fixture")
    monitor.stage("recovered", "image_cross_account_retry", index=1, attempt=2, failure_code="image_download_failed")
    monitor.stage("recovered", "image_single_done", index=1, attempt=2, status="success")
    image = monitor.call_detail("recovered")["images"]["1"]
    assert "failure_code" not in image


def test_legacy_log_restores_only_proven_final_attempt_and_shows_missing_time(log_api):
    logs, client = log_api
    postprocess = {"upscale_ms": 2397, "storage_ms": 65623, "postprocess_ms": 68043}
    first = {"account_wait_ms": 112, "egress_wait_ms": 5, "upload_ms": 1298,
             "bootstrap_ms": 10243, "requirements_ms": 405, "prepare_conversation_ms": 129,
             "generation_start_ms": 2267, "sse_stream_ms": 28336, "conversation_stream_ms": 42689,
             "poll_wait_ms": 3000, "poll_request_ms": 431, "resolve_ms": 1126, "download_ms": 931}
    second = {"account_wait_ms": 93, "egress_wait_ms": 6, "upload_ms": 1577,
              "bootstrap_ms": 11287, "requirements_ms": 642, "prepare_conversation_ms": 176,
              "generation_start_ms": 2198, "sse_stream_ms": 20964, "conversation_stream_ms": 36855,
              "poll_wait_ms": 3000, "poll_request_ms": 322, "resolve_ms": 495, "download_ms": 470,
              "total_ms": 219558}
    detail = {"call_id": "legacy", "status": "success", "endpoint": "/v1/images/edits",
              "duration_ms": 219606, "perf": postprocess,
              "image_attempts": [
                  {"slot": 1, "attempt": 1, "status": "failed", "duration_ms": 110166,
                   "monitor": {"metrics": first}},
                  {"slot": 1, "attempt": 2, "status": "success", "duration_ms": 109320,
                   "monitor": {"metrics": second}},
              ], "monitor": {"images": {"1": {"stage": "image_single_done", "status": "success",
                                                  "metrics": postprocess}},
                             "events": [{"event": "image_single_done", "index": 1, "attempt": 2, "status": "success"}]}}
    original = deepcopy(detail)
    logs.append_item({"id": "legacy", "type": "call", "detail": detail})
    response = client.get("/api/logs/legacy")
    assert response.status_code == 200, response.text
    attempts = response.json()["attempts"]
    assert "upscale_ms" not in attempts[0]["timings_ms"], "do not invent earlier failure's timing"
    assert attempts[1]["timings_ms"]["storage_ms"] == 65623
    for a in attempts:
        assert sum(s["value_ms"] for s in a["presentation"]["timeline"]["segments"]) == a["duration_ms"]
    assert detail == original
