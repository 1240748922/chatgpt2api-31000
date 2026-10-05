"""Synthetic terminal errors through parsing, retry, persistence and log APIs."""
import base64
import json
from collections import OrderedDict
from io import BytesIO
from queue import Queue
from types import SimpleNamespace

import pytest
from curl_cffi.requests.models import STREAM_END
from PIL import Image

from services import openai_backend_api as api
from services.image_failure import (
    ImageFailureError, ImageGenerationError, classify_message_facts,
    image_failure, public_image_error_message,
)
from services.protocol import conversation as protocol
from test_account_auth_quarantine import jwt, row, service_factory
from test_image_account_retries import retry_flow
from test_image_account_selection import _account
from test_image_recovery import backend, document
from test_log_detail_api import log_api


NOTICE = "由于我这边发生了错误，我未能生成图片。"
CODE = "upstream_image_generation_error"


def terminal_message(**fields):
    return {"id": "terminal-text", "author": {"role": "assistant"},
            "status": "finished_successfully", "end_turn": True,
            "content": {"content_type": "text", "parts": [NOTICE]}, **fields}


@pytest.mark.parametrize("overrides", [{"role": "user"}, {"status": "in_progress"},
                                     {"content_type": "code"}, {"has_image_output": True}])
def test_error_text_only_applies_to_terminal_assistant_image_text(overrides):
    fields = dict(role="assistant", content_type="text", status="finished_successfully",
                  has_text=True, raw_detail=NOTICE)
    failure = classify_message_facts(**{**fields, **overrides})
    assert failure is None or failure.code != CODE


@pytest.mark.parametrize("code", ["content_policy_violation", "invalid_image_input",
                                 "auth_invalid", "upstream_rate_limited"])
def test_structured_reason_overrides_generic_error_text(code):
    failure = classify_message_facts(role="assistant", content_type="text", end_turn=True,
                                     has_text=True, codes=[code], raw_detail=NOTICE)
    assert failure.code == code


def test_normal_chat_keeps_the_same_text_without_image_failure():
    payloads = [json.dumps({"conversation_id": "synthetic", "message": terminal_message()}), "[DONE]"]
    backend = SimpleNamespace(stream_conversation=lambda **kw: iter(payloads))
    events = list(protocol.conversation_events(backend, model="auto", prompt="synthetic"))
    assert events[-1]["text"] == NOTICE
    assert all(event.get("_image_failure") is None for event in events)


def test_public_error_does_not_leak_raw_technical_reply():
    failure = image_failure(CODE, raw_detail=NOTICE)
    error = ImageGenerationError(NOTICE, failure=failure, raw_upstream_message=NOTICE)
    assert public_image_error_message(failure, error) == "The image generation tool encountered an error. Please try again."
    assert error.raw_upstream_message == NOTICE


def test_editable_output_still_waits_for_file_after_image_error_text(backend, monkeypatch):
    instance, clock = backend
    documents = iter([{"mapping": {"m": {"message": terminal_message()}}}, {"mapping": {}}])
    outputs = iter([[], ["synthetic-output.pptx"]])
    monkeypatch.setattr(instance, "_get_editable_conversation_detail", lambda *a, **kw: next(documents))
    monkeypatch.setattr(instance, "_extract_editable_artifacts", lambda *a: next(outputs))
    monkeypatch.setattr(instance, "_pick_editable_target_artifacts", lambda artifacts, *a: artifacts)
    result = instance._wait_editable_output_artifacts("synthetic", "PowerPoint", (".pptx",),
        set(), (), None, timeout_secs=5, poll_interval_secs=1)
    assert result == ["synthetic-output.pptx"]
    assert clock.now == 1001


@pytest.mark.parametrize("mode, expected_attempts", [("limit", 4), ("disabled", 1), ("short_budget", 1)])
def test_technical_error_honors_retry_limits_and_releases_slots(retry_flow, mode, expected_attempts):
    if mode == "disabled":
        retry_flow.settings.image_account_retry_enabled = False
    with pytest.raises(ImageGenerationError) as caught:
        retry_flow.run([CODE] * 8, budget=20 if mode == "short_budget" else 120)
    assert caught.value.failure.code == CODE
    assert len(caught.value.image_attempts) == expected_attempts
    assert retry_flow.selected == retry_flow.closed == [str(i) for i in range(expected_attempts)]
    assert retry_flow.service._image_inflight == {}


def test_technical_error_does_not_quarantine_or_reduce_account_quota(service_factory, monkeypatch):
    token = jwt(86400)
    service = service_factory([row(token)])
    before = service.get_account(token)
    monkeypatch.setattr(service, "_schedule_image_failure_refresh_async",
                        lambda *a, **kw: pytest.fail("generic generation error is not auth evidence"))
    assert service.get_available_access_token() == token
    service.mark_image_result(token, False, failure=image_failure(CODE, raw_detail=NOTICE))
    after = service.get_account(token)
    for key in ("status", "quota", "image_quota_unknown", "last_remote_check_result", "fail"):
        assert after.get(key) == before.get(key)
    assert service._image_inflight == {}


@pytest.mark.parametrize("message_as_error", [False, True])
@pytest.mark.parametrize("first_result", ["error", "task_policy", "task_image", "sse_image"])
def test_sse_error_to_retry_and_saved_image(retry_flow, monkeypatch, tmp_path, message_as_error, first_result):
    retry_flow.service._accounts = OrderedDict((str(i), _account(str(i), quota=8, unknown=False)) for i in range(2))
    retry_flow.settings.image_poll_timeout_secs = 120
    retry_flow.settings.global_system_prompt = ""
    image_message = document(complete=True)["mapping"]["m"]["message"]
    png = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(png, format="PNG")
    data = png.getvalue()
    saved = tmp_path / "result.png"
    query_timeouts = []
    real_backend = api.OpenAIBackendAPI

    def create(access_token, **kwargs):
        retry_flow.selected.append(access_token)
        instance = real_backend.__new__(real_backend)
        instance.access_token = access_token
        instance.deadline_monotonic = kwargs.get("deadline_monotonic")
        instance.progress_callback = None
        instance.proxy_profile = SimpleNamespace()
        instance._http_timings = {}
        instance._closed = False
        def close():
            if not instance._closed:
                instance._closed = True
                retry_flow.closed.append(access_token)
        instance.close = close
        instance.adopt_page_prewarm = lambda: None
        instance._bootstrap = lambda **kw: None
        instance._get_chat_requirements = lambda **kw: None
        instance._prepare_image_conversation = lambda *a: ""
        first = access_token == "0"
        payloads = [{"conversation_id": "synthetic-" + access_token,
                     "message": terminal_message() if first else image_message}]
        if first and first_result == "sse_image":
            # Result arrives before the terminal text; it must remain deliverable.
            payloads.insert(0, {"conversation_id": "synthetic-0", "message": image_message})
        queue = Queue()
        for payload in payloads:
            chunk = ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()
            queue.put(chunk[:23])
            queue.put(chunk[23:])
        queue.put(b"data: [DONE]\n\n")
        queue.put(STREAM_END)
        instance._start_image_generation = lambda *a: SimpleNamespace(queue=queue, close=lambda: None)

        def query(**kw):
            query_timeouts.append(kw["timeout_secs"])
            assert first and kw["conversation_id"] == "synthetic-0"
            if first_result == "task_policy":
                return [{"image_gen_message": terminal_message(metadata={"error": {"code": "content_policy_violation"}})}]
            if first_result == "task_image":
                return [{"image_gen_message": image_message}]
            return []
        instance._query_backend_tasks = query
        instance.resolve_conversation_image_urls = lambda *a, **kw: ["https://example.invalid/image.png"]
        instance.download_image_bytes = lambda urls: [data]
        return instance

    def save(content, *a, **kw):
        saved.write_bytes(content)
        return "http://localhost/images/result.png"
    monkeypatch.setattr(protocol, "OpenAIBackendAPI", create)
    monkeypatch.setattr(protocol, "save_image_bytes", save)
    request = protocol.ConversationRequest(model="gpt-image-2", prompt="synthetic",
        response_format="b64_json", message_as_error=message_as_error)
    if first_result == "task_policy":
        if message_as_error:
            with pytest.raises(ImageGenerationError) as caught:
                protocol._generate_single_image(request, 1, 1)
            assert caught.value.failure.code == "content_policy_violation"
        else:
            outputs = protocol._generate_single_image(request, 1, 1)
            assert outputs[-1].failure.code == "content_policy_violation"
        assert not saved.exists()
    else:
        outputs = protocol._generate_single_image(request, 1, 1)
        result = next(output for output in outputs if output.kind == "result")
        assert saved.read_bytes() == base64.b64decode(result.data[0]["b64_json"]) == data
        assert result.image_attempts[-1]["status"] == "success"
        if first_result == "error":
            attempt = result.image_attempts[0]
            assert attempt["failure_code"] == CODE and attempt["switched_account"]
            assert attempt["raw_upstream_message"] == NOTICE
    expected = ["0", "1"] if first_result == "error" else ["0"]
    assert retry_flow.selected == retry_flow.closed == expected
    assert retry_flow.service._image_inflight == {}
    assert query_timeouts == ([] if first_result == "sse_image" else [5.0])


@pytest.mark.parametrize("task_result", ["none", "policy", "image", "query_failed"])
def test_poll_terminal_error_checks_task_evidence_once(backend, monkeypatch, task_result):
    instance, clock = backend
    monkeypatch.setattr(instance, "_get_conversation", lambda *a, **kw: {
        "mapping": {"m": {"message": terminal_message()}}})
    queries = []
    def query(**kw):
        queries.append(kw)
        if task_result == "query_failed":
            raise api.requests.exceptions.Timeout("synthetic task timeout")
        if task_result == "policy":
            return [{"image_gen_message": terminal_message(blocked=True)}]
        if task_result == "image":
            return [{"image_gen_message": document(complete=True)["mapping"]["m"]["message"]}]
        return []
    monkeypatch.setattr(instance, "_query_backend_tasks", query)
    if task_result == "image":
        files, _ = instance._poll_image_results("synthetic", timeout_secs=120)
        assert files == ["file_0123456789abcdef0123456789abcdef"]
    else:
        with pytest.raises(ImageFailureError) as caught:
            instance._poll_image_results("synthetic", timeout_secs=120)
        assert caught.value.failure.code == ("content_policy_violation" if task_result == "policy" else CODE)
        assert caught.value.raw_upstream_message == NOTICE
        assert caught.value.poll_attempts == 1
    assert len(queries) == 1 and queries[0]["timeout_secs"] == 3.0
    assert clock.now == 1000


@pytest.mark.parametrize("task_result", ["none", "policy", "image"])
def test_interrupted_stream_checks_task_before_retrying_technical_text(backend, monkeypatch, task_result):
    instance, clock = backend
    monkeypatch.setattr(protocol, "time", clock)
    monkeypatch.setattr(protocol, "config", SimpleNamespace(image_stream_timeout_secs=60, image_poll_timeout_secs=120))
    monkeypatch.setattr(instance, "_get_conversation", lambda *a, **kw: {
        "mapping": {"m": {"message": terminal_message()}}})
    queries = []
    def query(**kw):
        queries.append(kw)
        if task_result == "policy":
            return [{"image_gen_message": terminal_message(blocked=True)}]
        if task_result == "image":
            return [{"image_gen_message": document(complete=True)["mapping"]["m"]["message"]}]
        return []
    monkeypatch.setattr(instance, "_query_backend_tasks", query)
    monkeypatch.setattr(instance, "resolve_conversation_image_urls", lambda *a, **kw: ["https://example.invalid/image.png"])
    result = object()
    monkeypatch.setattr(protocol, "_image_result_output_from_urls", lambda *a, **kw: result)
    def recover():
        return protocol._recover_after_image_stream_timeout(instance,
            protocol.ConversationRequest(model="gpt-image-2", deadline_monotonic=1012),
            {"conversation_id": "synthetic"}, TimeoutError("SSE closed"), 1, 1, 950)
    if task_result == "image":
        assert recover() is result
    else:
        with pytest.raises(ImageGenerationError) as caught:
            recover()
        assert caught.value.failure.code == ("content_policy_violation" if task_result == "policy" else CODE)
        assert caught.value.raw_upstream_message == NOTICE
    assert len(queries) == 1 and queries[0]["timeout_secs"] <= 3
    assert clock.now == 1000


def test_failed_log_shows_technical_error_and_original_text(log_api):
    service, client = log_api
    failure = image_failure(CODE, raw_detail=NOTICE)
    detail = {"endpoint": "/v1/images/edits", "model": "gpt-image-2", "status": "failed",
              "duration_ms": 3000, "raw_upstream_message": NOTICE, **failure.diagnostic_fields()}
    detail["image_attempts"] = [{"attempt": 1, "slot": 1, **detail}]
    service.append_item({"id": "technical-error", "type": "call", "summary": "fixture", "detail": detail})
    response = client.get("/api/logs/technical-error")
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["attempts"][0]["outcome"] == "failed"
    assert view["attempts"][0]["status_code"] == 502
    assert view["attempts"][0]["presentation"]["failure_label"] == "上游图片生成失败"
    assert view["attempts"][0]["upstream_text"] == NOTICE
