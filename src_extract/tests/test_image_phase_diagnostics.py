from types import SimpleNamespace

import pytest

from services import openai_backend_api as api
from services.image_failure import ImageGenerationError, ImagePollTimeoutError, classify_image_exception
from services.log_service import _exception_log_fields, collect_image_attempts
from services.protocol import conversation as protocol
from services.realtime_monitor_service import RealtimeMonitorService
from services.request_detail_view import build_request_timeline_presentation
from test_image_recovery import Clock
from test_log_detail_api import log_api


@pytest.fixture
def phased_backend(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(api, "time", clock)
    monkeypatch.setattr(protocol, "time", clock)
    instance = api.OpenAIBackendAPI.__new__(api.OpenAIBackendAPI)
    instance.access_token = "test-token"
    instance.deadline_monotonic = None
    instance.progress_callback = None
    monkeypatch.setattr(instance, "_bootstrap", lambda **kw: clock.sleep(1))
    monkeypatch.setattr(instance, "_get_chat_requirements", lambda **kw: clock.sleep(2))
    monkeypatch.setattr(instance, "_prepare_image_conversation", lambda *a: clock.sleep(1))
    closed = []
    monkeypatch.setattr(instance, "_start_image_generation", lambda *a: SimpleNamespace(close=lambda: closed.append(True)))
    monkeypatch.setattr(instance, "_iter_timed_sse_payloads", lambda *a, **kw: iter(()))
    monkeypatch.setattr(instance, "_upload_image", lambda *a: None)
    return instance, clock, closed


@pytest.mark.parametrize("method,phase,metric,images", [
    ("_upload_image", "uploading", "upload_ms", ["input"]),
    ("_bootstrap", "bootstrapping", "bootstrap_ms", []),
    ("_get_chat_requirements", "getting_token", "requirements_ms", []),
    ("_prepare_image_conversation", "preparing_conversation", "prepare_conversation_ms", []),
    ("_start_image_generation", "starting_generation", "generation_start_ms", []),
])
def test_preparation_timeout_never_enters_stream_recovery(phased_backend, monkeypatch, method, phase, metric, images):
    instance, clock, _ = phased_backend
    def timeout(*a, **kw):
        clock.sleep(30)
        raise protocol.curl_exceptions.Timeout("curl (28): 30001 milliseconds with 0 bytes received")
    monkeypatch.setattr(instance, method, timeout)
    monkeypatch.setattr(protocol, "conversation_events", lambda backend, **kw: backend._stream_picture_conversation("test", "gpt-image-2", images))
    monkeypatch.setattr(protocol, "_recover_after_image_stream_timeout", lambda *a, **kw: pytest.fail("preparation must not poll"))
    with pytest.raises(protocol.curl_exceptions.Timeout) as caught:
        list(protocol.stream_image_outputs(instance, protocol.ConversationRequest()))
    assert classify_image_exception(caught.value).code == "upstream_connection_timeout"
    assert caught.value.failure_phase == phase
    timing = protocol._image_failure_timing_data(caught.value)
    assert timing[metric] == 30000
    assert "stream_error_ms" not in timing


def test_real_sse_timeout_recovers_and_does_not_count_preparation_or_polling(phased_backend, monkeypatch):
    instance, clock, closed = phased_backend
    def stream(*a, **kw):
        clock.sleep(10)
        raise TimeoutError("SSE read timed out")
        yield
    monkeypatch.setattr(instance, "_iter_timed_sse_payloads", stream)
    monkeypatch.setattr(protocol, "conversation_events", lambda backend, **kw: backend._stream_picture_conversation("test", "gpt-image-2", []))
    def recover(*a, **kw):
        clock.sleep(20)
        raise ImageGenerationError("SSE read timed out", code="image_stream_timeout")
    monkeypatch.setattr(protocol, "_recover_after_image_stream_timeout", recover)
    with pytest.raises(ImageGenerationError) as caught:
        list(protocol.stream_image_outputs(instance, protocol.ConversationRequest()))
    assert clock.now == 1034  # 4s preparation, 10s SSE, 20s recovery
    assert closed == [True]
    assert protocol._image_failure_timing_data(caught.value)["stream_error_ms"] == 10000


def test_two_attempts_keep_separate_errors_metrics_and_poll_diagnostics(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(protocol, "time", clock)
    monkeypatch.setattr(protocol, "config", SimpleNamespace(
        image_account_retry_enabled=True, image_max_account_attempts=2, image_stream_timeout_secs=90,
    ))
    tokens = iter(["one", "two"])
    outcomes, closed = [], []
    monkeypatch.setattr(protocol, "account_service", SimpleNamespace(
        get_available_access_token=lambda **kw: next(tokens),
        get_account=lambda token, **kw: {"email": token + "@example.test"},
        mark_image_result=lambda token, success, **kw: outcomes.append((token, success)),
    ))
    monkeypatch.setattr(protocol, "proxy_settings", SimpleNamespace(
        acquire_image_egress=lambda *a, **kw: 0, get_fallback_proxy_reference=lambda: "",
    ))
    class Backend:
        def __init__(self, access_token, **kw):
            self.token = access_token
            self.proxy_profile = SimpleNamespace()
        def close(self):
            closed.append(self.token)
    monkeypatch.setattr(protocol, "OpenAIBackendAPI", Backend)
    def output(backend, request, index, total):
        if backend.token == "one":
            clock.sleep(30)
            error = TimeoutError("bootstrap received no bytes")
            error.failure_phase = "bootstrapping"
            error.failure_phase_ms = 30000
            raise error
        clock.sleep(7)
        protocol._monitor_image_stage(request, "image_stream_resolve_start", index=index, total=total,
                                      conversation_stream_ms=7000, sse_stream_ms=3800)
        def resolve(*a, **kw):
            clock.sleep(120)
            error = ImagePollTimeoutError()
            error.conversation_id = "second-conversation"
            error.poll_attempts = 22
            error.poll_trace = [{"attempt": 22, "task_query": "ok", "task_count": 0}]
            raise error
        backend.resolve_conversation_image_urls = resolve
        protocol._resolve_image_urls_with_monitor(backend, request, "second-conversation", [], [], index, total)
        yield
    monkeypatch.setattr(protocol, "stream_image_outputs", output)
    monitor = RealtimeMonitorService()
    monkeypatch.setattr(protocol, "realtime_monitor_service", monitor)
    monitor.start("test-call", endpoint="/v1/images/generations", model="gpt-image-2")
    request = protocol.ConversationRequest(model="gpt-image-2", call_id="test-call", trace_image_perf=True)
    with pytest.raises(ImageGenerationError) as caught:
        protocol._generate_single_image(request, 1, 1)
    error = caught.value
    detail = {"call_id": "test-call", "status": "failed", "duration_ms": 157000,
              "error": str(error), **_exception_log_fields(error, image=True)}
    monitor.finish(detail)
    detail["image_attempts"] = collect_image_attempts(detail["image_attempts"])
    first, second = detail["image_attempts"]
    assert first["failure_code"] == "upstream_connection_timeout"
    assert first["failure_phase"] == "bootstrapping"
    assert first["monitor"]["metrics"]["bootstrap_ms"] == 30000
    assert first["raw_error"] == "bootstrap received no bytes"
    assert first["switched_account"] is True
    assert second["failure_code"] == "image_poll_timeout"
    assert second["failure_phase"] == "resolving"
    assert second["poll_trace"] == detail["poll_trace"]
    assert "raw_error" not in second
    assert "raw_error" not in detail["monitor"]
    assert "raw_error" not in detail["monitor"]["images"]["1"]
    assert "stream_error_ms" not in detail["monitor"]["metrics"]
    assert second["monitor"]["metrics"]["sse_stream_ms"] == 3800
    assert outcomes == [("one", False), ("two", False)]
    assert closed == ["one", "two"]
    timeline = build_request_timeline_presentation(first["monitor"]["metrics"], first["monitor"]["events"])
    assert not any(segment["key"] == "upstream" for segment in timeline["segments"])
    prepare = next(segment for segment in timeline["segments"] if segment["key"] == "prepare")
    assert prepare["tone"] == "danger"


def test_sse_failure_timeline_does_not_subtract_preparation_twice():
    timeline = build_request_timeline_presentation({"bootstrap_ms": 5000, "generation_start_ms": 1000, "stream_error_ms": 10000}, [])
    upstream = next(segment for segment in timeline["segments"] if segment["key"] == "upstream")
    assert upstream["value_ms"] == 11000


@pytest.mark.parametrize("failed", [False, True])
def test_plain_generation_bootstrap_retry_diagnostics_survive_log_api(phased_backend, log_api, monkeypatch, failed):
    from api.monitor_contract import MonitorEventView
    from services.monitor_view import _project_event
    instance, clock, _ = phased_backend
    calls = []
    def bootstrap(*, timeout_secs):
        calls.append(timeout_secs)
        clock.sleep(10 if len(calls) == 1 else 1)
        if len(calls) == 1 or failed:
            raise api.requests.exceptions.Timeout("synthetic page timeout")
    monkeypatch.setattr(instance, "_bootstrap", bootstrap)
    monitor = RealtimeMonitorService()
    monkeypatch.setattr(protocol, "realtime_monitor_service", monitor)
    monitor.start("warmup", endpoint="/v1/images/generations", model="fixture")
    request = protocol.ConversationRequest(call_id="warmup", trace_image_perf=True)
    instance.progress_callback = protocol._image_progress_callback_with_monitor(
        request, 1, 1, lambda: "", instance.image_input_timings)
    if failed:
        with pytest.raises(api.requests.exceptions.Timeout) as caught:
            list(instance._stream_picture_conversation("test", "gpt-image-2", []))
        metrics = protocol._image_failure_timing_data(caught.value)
    else:
        list(instance._stream_picture_conversation("test", "gpt-image-2", []))
        event = next(e for e in monitor._events if e["event"] == "image_getting_token")
        view = MonitorEventView.model_validate(_project_event(event))
        assert view.bootstrap_first_ms == 10000 and view.bootstrap_retry_ms == 1000
        metrics = {k: v for k, v in event.items() if k.endswith("_ms")}
    assert metrics["bootstrap_ms"] == 11000
    assert metrics["bootstrap_first_ms"] == 10000
    assert metrics["bootstrap_retry_ms"] == 1000
    logs, client = log_api
    logs.append_item({"id": "warmup", "type": "call", "detail": {
        "call_id": "warmup", "status": "failed" if failed else "success", "duration_ms": 11000,
        "endpoint": "/v1/images/generations", "image_attempts": [{"slot": 1, "attempt": 1,
            "duration_ms": 11000, "monitor": {"metrics": metrics}}]}})
    response = client.get("/api/logs/warmup")
    assert response.status_code == 200, response.text
    timings = response.json()["attempts"][0]["timings_ms"]
    assert timings["bootstrap_first_ms"] == 10000 and timings["bootstrap_retry_ms"] == 1000
    timeline = build_request_timeline_presentation(metrics, [], wall_duration_ms=11000)
    assert {s["key"]: s["value_ms"] for s in timeline["segments"]} == {"prepare": 11000}
