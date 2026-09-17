"""Validate persisted logs through real FastAPI response-model serialization."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import system
from services.call_record_service import CallRecordService
from services.realtime_monitor_service import RealtimeMonitorService
from services.request_detail_view import build_request_timeline_presentation, _TIMELINE_STEPS
from api.request_detail_contract import RequestTimelinePresentation


@pytest.fixture
def log_api(tmp_path, monkeypatch):
    service = CallRecordService(database_url=f"sqlite:///{(tmp_path / 'logs.db').as_posix()}")
    monkeypatch.setattr(system, "log_service", service)
    monkeypatch.setattr(system, "require_admin", lambda auth: None)
    app = FastAPI()
    app.include_router(system.create_router("test"))
    return service, TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("postprocess", [False, True])
def test_persisted_log_list_and_detail_with_postprocess(log_api, postprocess):
    service, client = log_api
    metrics = {"sse_stream_ms": 31000}
    if postprocess:
        metrics.update(upscale_ms=200000, storage_ms=1000, postprocess_ms=201000)
    service.append_item({"id": "image-log", "type": "call", "summary": "fixture", "detail": {
        "endpoint": "/v1/images/edits", "model": "gpt-image-2", "status": "success",
        "duration_ms": 232000, "metrics": metrics,
        "image_attempts": [{"slot": 1, "attempt": 1, "status": "success",
                            "duration_ms": 232000, "monitor": {"metrics": metrics}}],
    }})
    assert client.get("/api/logs").status_code == 200
    response = client.get("/api/logs/image-log")
    assert response.status_code == 200, response.text
    detail = response.json()
    if postprocess:
        assert any(g["key"] == "postprocess" for g in detail["detail_presentation"]["timeline"]["groups"])
        assert detail["attempts"][0]["timings_ms"]["upscale_ms"] == 200000
    assert client.get("/api/logs/missing").status_code == 404


def test_live_monitor_detail_accepts_postprocess(log_api, monkeypatch):
    _, client = log_api
    monitor = RealtimeMonitorService()
    monitor.start("live", endpoint="/v1/images/generations", model="gpt-image-2")
    monitor.stage("live", "image_postprocess_done", upscale_ms=5000, postprocess_ms=5500)
    monkeypatch.setattr(system, "cluster_monitor_call_detail", monitor.call_detail)
    response = client.get("/api/monitor/realtime/live")
    assert response.status_code == 200, response.text


def test_all_timeline_metrics_conform_to_api_contract():
    # Adding a new view category must update both logs and monitor contracts.
    timeline = build_request_timeline_presentation({item[0]: 1000 for item in _TIMELINE_STEPS}, [])
    RequestTimelinePresentation.model_validate(timeline)
