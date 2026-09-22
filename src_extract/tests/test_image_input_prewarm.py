"""Real-thread synthetic transport tests. No network or real accounts."""
import base64
import threading
import time
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image
from curl_cffi.requests.cookies import Cookies

from services import openai_backend_api as api
from services import image_input_prewarm as prewarm
from services.image_failure import classify_image_exception
from services.protocol import conversation as protocol
from services.request_detail_view import build_request_timeline_presentation
from test_log_detail_api import log_api


def response(code=200, data=None, text=""):
    return SimpleNamespace(status_code=code, headers={}, text=text, json=lambda: data or {})


@pytest.fixture
def transport(monkeypatch):
    pools = []
    def build(*, parallel=True, fail="", capacity=1, deadline=None, confirm_delay=0, warm_delay=.04):
        pool = prewarm.NonQueuingPool(capacity)
        pools.append(pool)
        monkeypatch.setattr(api, "input_transfer_pool", pool)
        state = SimpleNamespace(events=[], workers=[], put_started=threading.Event(),
                                warm_started=threading.Event(), timeouts=[], payloads=[], session_kwargs=[],
                                caller=threading.get_ident(), registrations=0)
        data = BytesIO()
        Image.new("RGB", (4, 6), "red").save(data, format="PNG")
        state.bytes = data.getvalue()
        state.input = base64.b64encode(state.bytes).decode()

        def delay(seconds, timeout):
            state.timeouts.append(timeout)
            if timeout < seconds:
                time.sleep(max(0, timeout))
                raise api.requests.exceptions.Timeout("synthetic deadline")
            time.sleep(seconds)

        class MainSession:
            headers = {"Sec-Ch-Ua": "synthetic", "Sec-Ch-Ua-Mobile": "?0", "Sec-Ch-Ua-Platform": "test",
                       "User-Agent": "synthetic-UA", "OAI-Session-Id": "synthetic-session"}
            curl_options = {}
            def __init__(self):
                self.cookies = Cookies({"registration": "account-cookie"})
            def post(self, url, **kwargs):
                assert threading.get_ident() == state.caller
                assert kwargs["headers"]["Authorization"] == "Bearer synthetic-at"
                state.timeouts.append(kwargs["timeout"])
                if url.endswith("/files"):
                    state.events.append("register")
                    state.registrations += 1
                    if fail == "register401":
                        return response(401, {"error": {"code": "token_invalid"}})
                    if fail == "register429":
                        return response(429, {"error": {"code": "file_upload_throttled"}})
                    return response(data={"upload_url": "https://asset.invalid/signed", "file_id": f"file-{state.registrations}"})
                assert url.endswith("/uploaded")
                assert all(worker.closed for worker in state.workers)
                state.events.append("confirm")
                delay(confirm_delay, kwargs["timeout"])
                if fail == "confirm401":
                    return response(401, {"error": {"code": "token_invalid"}})
                return response(429, {"error": {"code": "file_upload_throttled"}}) if fail == "confirm429" else response()
            def put(self, url, **kwargs):
                assert threading.get_ident() == state.caller
                state.events.append("serial_put")
                state.payloads.append(kwargs["data"])
                delay(.06, kwargs["timeout"])
                return response()
            def get(self, url, **kwargs):
                assert threading.get_ident() == state.caller
                state.events.append("bootstrap")
                state.warm_started.set()
                if parallel:
                    assert state.put_started.wait(2), "PUT was not started concurrently"
                delay(warm_delay, kwargs["timeout"])
                if fail in {"bootstrap", "both"}:
                    return response(403, {"detail": "synthetic warmup rejected"})
                self.cookies["warmup"] = "session-local"
                state.events.append("bootstrap_done")
                return response(text='<script src="https://chatgpt.com/synthetic.js"></script>')
            def close(self):
                state.events.append("main_closed")

        class TransferSession:
            def __init__(self, **kwargs):
                self.closed = False
                self.cookies = Cookies()
                self.headers = {}
                state.workers.append(self)
                state.session_kwargs.append(kwargs)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.closed = True
                state.events.append("transfer_closed")
            def put(self, url, **kwargs):
                assert threading.get_ident() != state.caller
                assert not self.cookies and not self.headers
                assert not {key.lower() for key in kwargs["headers"]} & {"cookie", "authorization", "oai-session-id"}
                assert kwargs["headers"]["User-Agent"] == "synthetic-UA"
                state.events.append("parallel_put")
                state.payloads.append(kwargs["data"])
                state.put_started.set()
                assert state.warm_started.wait(2), "bootstrap did not overlap"
                delay(.06, kwargs["timeout"])
                return response(403, {"detail": "synthetic signed asset failed"}) if fail in {"put", "both"} else response()
            def post(self, url, **kwargs):
                assert threading.get_ident() != state.caller
                assert url == "https://chatgpt.com/backend-api/files/file-1/uploaded"
                assert kwargs["headers"]["Authorization"] == "Bearer synthetic-at"
                assert self.headers["OAI-Session-Id"] == "synthetic-session"
                assert self.cookies["registration"] == "account-cookie"
                state.events.append("confirm")
                delay(confirm_delay, kwargs["timeout"])
                self.cookies["confirmed"] = "worker-cookie"
                if fail == "confirm401":
                    return response(401, {"error": {"code": "token_invalid"}})
                return response(429, {"error": {"code": "file_upload_throttled"}}) if fail == "confirm429" else response()

        monkeypatch.setattr(prewarm.requests, "Session", TransferSession)
        instance = api.OpenAIBackendAPI.__new__(api.OpenAIBackendAPI)
        instance.access_token = "synthetic-at"
        instance.user_agent = "synthetic-UA"
        instance.base_url = "https://chatgpt.com"
        instance.fp = {"impersonate": "chrome110"}
        instance.proxy_profile = SimpleNamespace(proxy_url="http://synthetic-proxy.invalid:8080", skip_ssl_verify=False)
        instance.session = MainSession()
        instance.progress_callback = None
        instance.deadline_monotonic = time.monotonic() + deadline if deadline is not None else None
        def requirements(**kwargs):
            assert all(worker.closed for worker in state.workers)
            assert state.events.count("confirm") == state.registrations
            assert instance.session.cookies["warmup"] == "session-local"
            if state.workers:
                assert instance.session.cookies["confirmed"] == "worker-cookie"
            assert instance.pow_script_sources
            state.events.append("requirements")
        monkeypatch.setattr(instance, "_get_chat_requirements", requirements)
        monkeypatch.setattr(instance, "_prepare_image_conversation", lambda *a: state.events.append("prepare"))
        def generate(*args):
            state.events.append("generate")
            return SimpleNamespace(close=lambda: state.events.append("sse_closed"))
        monkeypatch.setattr(instance, "_start_image_generation", generate)
        monkeypatch.setattr(instance, "_iter_timed_sse_payloads", lambda *a, **kw: iter(()))
        return instance, state, pool
    yield build
    for pool in pools:
        pool._executor.shutdown(wait=True)


def run(instance, state, count=1):
    return list(instance._stream_picture_conversation("synthetic", "gpt-image-2", [state.input] * count))


def test_upload_and_bootstrap_overlap_without_sharing_session(transport):
    instance, state, _ = transport()
    run(instance, state)
    assert state.events.count("bootstrap") == 1
    assert state.events.index("register") < state.events.index("bootstrap")
    assert state.events.index("confirm") < state.events.index("transfer_closed") < state.events.index("requirements")
    assert state.events.count("generate") == 1
    assert state.payloads == [state.bytes]
    assert state.session_kwargs == [{"proxy": "http://synthetic-proxy.invalid:8080", "impersonate": "chrome110", "verify": True}]
    m = instance.image_input_timings()
    assert m["upload_put_ms"] >= 50 and m["bootstrap_ms"] >= 30
    assert m["prewarm_overlap_ms"] > 0
    assert abs(m["upload_ms"] + m["bootstrap_ms"] - m["prewarm_overlap_ms"] - m["input_prepare_ms"]) < 30


@pytest.mark.parametrize("mode", ["disabled", "full"])
def test_no_capacity_falls_back_to_serial_without_waiting(transport, mode):
    instance, state, pool = transport(parallel=False, capacity=0 if mode == "disabled" else 1)
    release = threading.Event()
    held = pool.try_submit(lambda: release.wait(3)) if mode == "full" else None
    try:
        run(instance, state)
        assert not state.workers
        assert state.events.index("serial_put") < state.events.index("confirm") < state.events.index("bootstrap")
        assert not held or not held.done()  # request did not wait behind the occupied worker
        assert instance.image_input_timings().get("prewarm_overlap_ms", 0) == 0
    finally:
        release.set()


@pytest.mark.parametrize("failure", ["register401", "register429", "put", "bootstrap", "both", "confirm429", "confirm401"])
def test_failure_preserves_phase_closes_transfer_and_never_submits_generation(transport, failure):
    instance, state, pool = transport(fail=failure)
    with pytest.raises(Exception) as caught:
        run(instance, state)
    expected_phase = "bootstrapping" if failure == "bootstrap" else "uploading"
    assert caught.value.failure_phase == expected_phase
    assert "generate" not in state.events and "requirements" not in state.events
    assert all(worker.closed for worker in state.workers)
    if failure.startswith("register"):
        assert "bootstrap" not in state.events and not state.workers
    timing = protocol._image_failure_timing_data(caught.value)
    assert "input_prepare_ms" in timing
    if failure in {"register429", "confirm429"}:
        assert classify_image_exception(caught.value).code == "file_upload_throttled"
    if failure in {"register401", "confirm401"}:
        assert classify_image_exception(caught.value).code == "auth_invalid"
    # Future capacity is returned once its cleanup has finished, including failures.
    pool._executor.shutdown(wait=True)
    assert pool._slots.acquire(blocking=False)
    pool._slots.release()


def test_all_inputs_remain_ordered_and_bootstrap_happens_only_once(transport):
    instance, state, _ = transport()
    run(instance, state, count=3)
    assert state.events.count("bootstrap") == 1
    assert state.events.count("parallel_put") == 1 and state.events.count("serial_put") == 2
    assert state.events.count("confirm") == 3
    assert state.payloads == [state.bytes] * 3


def test_no_reference_images_keep_original_flow(transport):
    instance, state, _ = transport(parallel=False)
    run(instance, state, count=0)
    assert not state.workers
    assert state.events == ["bootstrap", "bootstrap_done", "requirements", "prepare", "generate", "sse_closed"]
    assert instance.image_input_timings() == {}


def test_request_deadline_bounds_both_operations_and_drains_worker(transport):
    instance, state, _ = transport(deadline=.03)
    with pytest.raises(Exception):
        run(instance, state)
    assert all(0 < timeout <= .03 for timeout in state.timeouts)
    assert all(worker.closed for worker in state.workers)
    assert "requirements" not in state.events and "generate" not in state.events


def test_worker_does_not_open_session_after_expired_deadline(monkeypatch):
    monkeypatch.setattr(prewarm.requests, "Session", lambda **kw: pytest.fail("expired worker opened a session"))
    timing = {}
    with pytest.raises(Exception) as caught:
        prewarm.put_signed_image({}, "https://asset.invalid", {}, b"", 120, time.monotonic() - 1, timing)
    assert classify_image_exception(caught.value).code == "task_interrupted"
    assert timing["ended"] >= timing["started"]


def test_parallel_metrics_reach_monitor_without_double_counting(transport, monkeypatch):
    from services.realtime_monitor_service import RealtimeMonitorService
    from services.monitor_view import _project_event
    from api.monitor_contract import MonitorEventView
    instance, state, _ = transport()
    monitor = RealtimeMonitorService()
    monkeypatch.setattr(protocol, "realtime_monitor_service", monitor)
    monitor.start("input", endpoint="/v1/images/edits", model="fixture")
    request = protocol.ConversationRequest(call_id="input", trace_image_perf=True)
    instance.progress_callback = protocol._image_progress_callback_with_monitor(
        request, 1, 1, lambda: "", instance.image_input_timings)
    run(instance, state)
    event = next(e for e in monitor._events if e["event"] == "image_getting_token")
    view = MonitorEventView.model_validate(_project_event(event))
    assert view.prewarm_overlap_ms > 0 and view.input_prepare_ms > 0
    for key, value in instance.image_input_timings().items():
        assert event[key] == value
    # Exact synthetic durations: 4s upload + 3s warmup with 2s overlap = 5s wall.
    metrics = {"input_prepare_ms": 5000, "upload_ms": 4000, "bootstrap_ms": 3000,
               "prewarm_overlap_ms": 2000, "requirements_ms": 1000, "prepare_conversation_ms": 500,
               "conversation_stream_ms": 26500}
    timeline = build_request_timeline_presentation(metrics, [], wall_duration_ms=26500)
    segments = {s["key"]: s["value_ms"] for s in timeline["segments"]}
    assert segments == {"prepare": 6500, "upstream": 20000}


def test_parallel_preparation_delivers_real_image_bytes(transport, monkeypatch, tmp_path):
    import json
    instance, state, _ = transport()
    file_id = "file_000000000123456789abcdef01234567"
    payloads = [json.dumps({"conversation_id": "synthetic-conversation", "message": {
        "author": {"role": "tool"}, "status": "finished_successfully", "end_turn": False,
        "metadata": {"async_task_type": "image_gen"},
        "content": {"content_type": "multimodal_text", "parts": [{
            "content_type": "image_asset_pointer", "asset_pointer": "file-service://" + file_id,
        }]},
    }}), "[DONE]"]
    monkeypatch.setattr(instance, "_iter_timed_sse_payloads", lambda *a, **kw: iter(payloads))
    def resolve(cid, files, sediments, **kw):
        assert cid == "synthetic-conversation" and files == [file_id]
        return ["https://asset.invalid/output.png"]
    monkeypatch.setattr(instance, "resolve_conversation_image_urls", resolve)
    monkeypatch.setattr(instance, "download_image_bytes", lambda urls: [state.bytes])
    path = tmp_path / "result.png"
    def save(data, *a, **kw):
        path.write_bytes(data)
        return "http://localhost/images/result.png"
    monkeypatch.setattr(protocol, "save_image_bytes", save)
    outputs = list(protocol.stream_image_outputs(instance, protocol.ConversationRequest(
        model="gpt-image-2", prompt="synthetic", images=[state.input], response_format="b64_json")))
    result = next(output for output in outputs if output.kind == "result")
    assert base64.b64decode(result.data[0]["b64_json"]) == path.read_bytes() == state.bytes
    assert result.data[0]["width"] == 4 and result.data[0]["height"] == 6
    assert state.events.count("generate") == 1


def test_pool_submission_failure_releases_slot(monkeypatch):
    pool = prewarm.NonQueuingPool(1)
    pool._executor.shutdown(wait=True)
    assert pool.try_submit(lambda: None) is None
    assert pool._slots.acquire(blocking=False)
    pool._slots.release()


def test_upload_confirmation_overlaps_page_warmup_not_just_bytes(transport):
    instance, state, _ = transport(confirm_delay=.12, warm_delay=.14)
    run(instance, state)
    assert state.events.index("confirm") < state.events.index("bootstrap_done")
    assert state.events.index("bootstrap_done") < state.events.index("transfer_closed")
    timings = instance.image_input_timings()
    assert timings["upload_confirm_ms"] >= 100
    assert timings["prewarm_overlap_ms"] > timings["upload_put_ms"] + 30
    assert abs(timings["upload_ms"] + timings["bootstrap_ms"] - timings["prewarm_overlap_ms"]
               - timings["input_prepare_ms"]) < 30


def test_confirmation_deadline_drains_worker_and_does_not_generate(transport):
    instance, state, _ = transport(confirm_delay=.12, warm_delay=.02, deadline=.11)
    with pytest.raises(Exception) as caught:
        run(instance, state)
    assert caught.value.failure_phase == "uploading"
    assert "confirm" in state.events and "generate" not in state.events
    assert all(worker.closed for worker in state.workers)
    assert all(0 < timeout <= .11 for timeout in state.timeouts)
    assert caught.value.image_input_timings["upload_confirm_ms"] > 0


def test_confirmation_cookie_merge_preserves_concurrent_warmup_and_cookie_scope():
    from copy import deepcopy
    main = Cookies()
    for name in ("unchanged", "update", "conflict", "remove"):
        main.set(name, "before", domain="chatgpt.com", path="/backend-api", secure=True)
    baseline = prewarm.snapshot_cookies(main)
    worker = Cookies()
    for cookie in baseline.values():
        worker.jar.set_cookie(deepcopy(cookie))
    worker.set("update", "confirmed", domain="chatgpt.com", path="/backend-api", secure=True)
    worker.set("conflict", "worker", domain="chatgpt.com", path="/backend-api", secure=True)
    worker.delete("remove", domain="chatgpt.com", path="/backend-api")
    worker.set("added", "worker", domain="chatgpt.com", path="/backend-api", secure=True)
    main.set("conflict", "warmup", domain="chatgpt.com", path="/backend-api", secure=True)
    main.set("warmup-only", "main", domain="chatgpt.com", path="/", secure=True)
    prewarm.merge_upload_cookies(main, baseline, prewarm.snapshot_cookies(worker))
    assert main.get("update") == "confirmed" and main.get("conflict") == "warmup"
    assert main.get("remove") is None and main.get("added") == "worker"
    assert main.get("warmup-only") == "main" and main.get("unchanged") == "before"
    assert all(cookie.secure for cookie in main.jar)
    assert next(cookie for cookie in main.jar if cookie.name == "update").path == "/backend-api"
    assert next(cookie for cookie in main.jar if cookie.name == "update").domain == "chatgpt.com"


def test_real_loopback_http_confirmation_and_warmup_keep_session_identity(monkeypatch):
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    warm_started, confirm_started = threading.Event(), threading.Event()
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def reply(self, data, cookie=""):
            encoded = data.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(encoded)))
            if cookie:
                self.send_header("Set-Cookie", cookie + "; Path=/")
            self.end_headers()
            self.wfile.write(encoded)
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append((self.path, dict(self.headers)))
            if self.path.endswith("/files"):
                self.reply(json.dumps({"file_id": "file-1", "upload_url": base + "/asset"}), "registered=1")
            else:
                confirm_started.set()
                assert warm_started.wait(3)
                self.reply("{}", "confirmed=1")
        def do_PUT(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append((self.path, dict(self.headers)))
            self.reply("{}")
        def do_GET(self):
            warm_started.set()
            assert confirm_started.wait(3), "confirmation was still serialized behind warmup"
            self.reply('<script src="https://chatgpt.com/synthetic.js"></script>', "warmed=1")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    pool = prewarm.NonQueuingPool(1)
    monkeypatch.setattr(api, "input_transfer_pool", pool)
    instance = api.OpenAIBackendAPI.__new__(api.OpenAIBackendAPI)
    instance.access_token = "synthetic-only"
    instance.base_url = base
    instance.user_agent = "test-UA"
    instance.fp = {"impersonate": "chrome110"}
    instance.proxy_profile = SimpleNamespace(proxy_url="", skip_ssl_verify=False)
    instance.deadline_monotonic = time.monotonic() + 10
    instance.progress_callback = None
    instance.session = api.requests.Session(trust_env=False)
    instance.session.headers.update({"User-Agent": "test-UA", "OAI-Session-Id": "test-session",
        "Sec-Ch-Ua": "test", "Sec-Ch-Ua-Mobile": "?0", "Sec-Ch-Ua-Platform": "test"})
    original_kwargs = api.proxy_settings.build_session_kwargs_from_profile
    monkeypatch.setattr(api.proxy_settings, "build_session_kwargs_from_profile",
                        lambda *a, **kw: {**original_kwargs(*a, **kw), "trust_env": False})
    generated = []
    def requirements(**kw):
        assert instance.session.cookies.get("registered") == "1"
        assert instance.session.cookies.get("confirmed") == "1"
        assert instance.session.cookies.get("warmed") == "1"
    instance._get_chat_requirements = requirements
    instance._prepare_image_conversation = lambda *a: None
    instance._start_image_generation = lambda *a: generated.append(True) or SimpleNamespace(close=lambda: None)
    instance._iter_timed_sse_payloads = lambda *a, **kw: iter(())
    data = BytesIO()
    Image.new("RGB", (2, 2)).save(data, format="PNG")
    try:
        list(instance._stream_picture_conversation("synthetic", "gpt-image-2", [base64.b64encode(data.getvalue()).decode()]))
        assert generated == [True]
        requests_by_path = {path: {k.lower(): v for k, v in headers.items()} for path, headers in seen}
        asset = requests_by_path["/asset"]
        assert not set(asset) & {"cookie", "authorization", "oai-session-id"}
        confirmed = requests_by_path["/backend-api/files/file-1/uploaded"]
        assert confirmed["authorization"] == "Bearer synthetic-only"
        assert confirmed["oai-session-id"] == "test-session" and "registered=1" in confirmed["cookie"]
    finally:
        pool._executor.shutdown(wait=True)
        instance.session.close()
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_failed_preparation_metrics_survive_generation_attempt_and_log_api(transport, log_api, monkeypatch):
    from services.image_failure import ImageGenerationError
    from services.log_service import _exception_log_fields
    from services.realtime_monitor_service import RealtimeMonitorService
    instance, state, _ = transport(fail="bootstrap")
    logs, client = log_api
    monitor = RealtimeMonitorService()
    monkeypatch.setattr(protocol, "realtime_monitor_service", monitor)
    monkeypatch.setattr(protocol, "OpenAIBackendAPI", lambda **kw: instance)
    monkeypatch.setattr(protocol, "config", SimpleNamespace(image_account_retry_enabled=False,
        image_max_account_attempts=1, image_stream_timeout_secs=90, global_system_prompt=""))
    monkeypatch.setattr(protocol, "account_service", SimpleNamespace(
        get_available_access_token=lambda **kw: "synthetic-at", get_account=lambda *a, **kw: {},
        mark_image_result=lambda *a, **kw: None))
    monkeypatch.setattr(protocol, "proxy_settings", SimpleNamespace(
        acquire_image_egress=lambda *a, **kw: 0, get_fallback_proxy_reference=lambda: ""))
    monitor.start("input-failure", endpoint="/v1/images/edits", model="fixture")
    request = protocol.ConversationRequest(model="gpt-image-2", images=[state.input],
                                           call_id="input-failure", trace_image_perf=True)
    with pytest.raises(ImageGenerationError) as caught:
        protocol._generate_single_image(request, 1, 1)
    detail = {"call_id": "input-failure", "status": "failed", "endpoint": "/v1/images/edits",
              "duration_ms": 100, **_exception_log_fields(caught.value, image=True)}
    monitor.finish(detail)
    logs.append_item({"id": "input-failure", "type": "call", "detail": detail})
    response = client.get("/api/logs/input-failure")
    assert response.status_code == 200, response.text
    attempt = response.json()["attempts"][0]
    assert attempt["timings_ms"]["input_prepare_ms"] > 0
    assert attempt["timings_ms"]["prewarm_overlap_ms"] > 0
    assert attempt["timings_ms"]["bootstrap_ms"] > 0
    assert "generate" not in state.events
