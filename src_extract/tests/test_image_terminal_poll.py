"""Synthetic replays of quota prose and a stable empty completed turn.

No account identifiers, credentials, or customer prompts are kept here.
"""
from copy import deepcopy

import pytest

from services.image_failure import ImageFailureError, ImagePollTimeoutError, classify_upstream_message
from test_image_recovery import backend, document
from test_image_account_retries import retry_flow


DEFERRED_QUOTA = "免费安排稍后生成此图像。升级订阅即可立即创建，或在用量于 明天 17:53 重置后重试。"


def message(role, kind, text="", **kw):
    return {"author": {"role": role}, "status": "finished_successfully",
            "content": {"content_type": kind, "parts": [text]}, **kw}


def empty_completed():
    return {"current_node": "recap", "mapping": {
        "user": {"parent": None, "message": message("user", "text", "synthetic image request")},
        "empty": {"parent": "user", "message": message("assistant", "text", id="empty")},
        "recap": {"parent": "empty", "message": message("assistant", "reasoning_recap", id="recap")},
    }}


@pytest.mark.parametrize("role", ["tool", "assistant"])
def test_deferred_free_image_is_account_quota_not_poll_timeout(role):
    failure = classify_upstream_message(message(role, "text", DEFERRED_QUOTA))
    assert failure is not None
    assert failure.code == "image_quota_exhausted"
    assert failure.account_capacity_limited and failure.switch_account


@pytest.mark.parametrize("role,kind,text", [
    ("assistant", "text", "我可以稍后生成此图像。"),
    ("tool", "text", "升级订阅即可立即创建更多图像。"),
    ("tool", "text", "用量将于明天重置，稍后重试。"),
    ("user", "text", DEFERRED_QUOTA),
    ("assistant", "code", DEFERRED_QUOTA),
])
def test_upgrade_or_quoted_prompt_alone_is_not_new_quota_signal(role, kind, text):
    failure = classify_upstream_message(message(role, kind, text))
    assert failure is None or failure.code != "image_quota_exhausted"


def test_poll_stops_at_confirmed_quota_without_task_query(backend, monkeypatch):
    instance, clock = backend
    pending = document()
    limited = deepcopy(pending)
    limited["mapping"]["tool"] = {"message": message("tool", "text", DEFERRED_QUOTA, create_time=2)}
    calls = []
    monkeypatch.setattr(instance, "_get_conversation", lambda *a, **kw: pending if clock.now < 1004 else limited)
    monkeypatch.setattr(instance, "_query_backend_tasks", lambda **kw: calls.append(clock.now) or [])
    with pytest.raises(ImageFailureError) as caught:
        instance._poll_image_results("synthetic", timeout_secs=120)
    assert caught.value.failure.code == "image_quota_exhausted"
    assert clock.now == 1004
    assert calls == [1000, 1001, 1002, 1003]
    assert caught.value.poll_trace[-1]["failure_code"] == "image_quota_exhausted"


def test_stable_empty_turn_retries_after_grace_not_full_poll_budget(backend, monkeypatch):
    instance, clock = backend
    monkeypatch.setattr(instance, "_get_conversation", lambda *a, **kw: empty_completed())
    monkeypatch.setattr(instance, "_query_backend_tasks", lambda **kw: [])
    with pytest.raises(ImageFailureError) as caught:
        instance._poll_image_results("synthetic", timeout_secs=120)
    assert caught.value.failure.code == "no_image_generated"
    assert caught.value.failure.switch_account
    assert not caught.value.failure.account_capacity_limited
    assert clock.now == 1015
    assert caught.value.poll_trace[-1]["empty_terminal_ms"] == 15000


@pytest.mark.parametrize("change", ["running", "code", "tool_recipient", "no_recap", "task_active", "task_unavailable"])
def test_ambiguous_or_active_turn_is_not_short_circuited(backend, monkeypatch, change):
    instance, clock = backend
    doc = empty_completed()
    empty = doc["mapping"]["empty"]["message"]
    if change == "running":
        empty["status"] = "in_progress"
    elif change == "code":
        empty["content"] = {"content_type": "code", "text": '{"prompt":"a tree"}'}
    elif change == "tool_recipient":
        empty["recipient"] = "image_gen.text2im"
    elif change == "no_recap":
        doc["current_node"] = "empty"
        doc["mapping"].pop("recap")
    def tasks(**kw):
        if change == "task_unavailable":
            raise RuntimeError("task API temporarily unavailable")
        return [{"status": "running"}] if change == "task_active" else []
    monkeypatch.setattr(instance, "_get_conversation", lambda *a, **kw: doc)
    monkeypatch.setattr(instance, "_query_backend_tasks", tasks)
    with pytest.raises(ImagePollTimeoutError):
        instance._poll_image_results("synthetic", timeout_secs=20)
    assert clock.now == 1020


def test_image_arriving_during_empty_grace_is_delivered(backend, monkeypatch):
    instance, clock = backend
    monkeypatch.setattr(instance, "_get_conversation", lambda *a, **kw:
                        empty_completed() if clock.now < 1014 else document(complete=True))
    monkeypatch.setattr(instance, "_query_backend_tasks", lambda **kw: [])
    files, _ = instance._poll_image_results("synthetic", timeout_secs=120)
    assert files
    assert clock.now == 1014


def test_explicit_reference_request_remains_text_not_quota_or_empty(backend, monkeypatch):
    instance, clock = backend
    doc = empty_completed()
    doc["mapping"]["empty"]["message"]["content"]["parts"] = ["Please upload the reference image."]
    monkeypatch.setattr(instance, "_get_conversation", lambda *a, **kw: doc)
    monkeypatch.setattr(instance, "_query_backend_tasks", lambda **kw: pytest.fail("terminal text already available"))
    with pytest.raises(ImageFailureError) as caught:
        instance._poll_image_results("synthetic", timeout_secs=120)
    assert caught.value.failure.code == "upstream_text_reply"
    assert not caught.value.failure.switch_account
    assert clock.now == 1000


@pytest.mark.parametrize("interruption", ["changed_node", "task", "query_error"])
def test_empty_grace_resets_on_state_change_or_uncertain_query(backend, monkeypatch, interruption):
    instance, clock = backend
    def get(*a, **kw):
        if interruption == "query_error" and clock.now == 1010:
            from utils.helper import UpstreamHTTPError
            raise UpstreamHTTPError("conversation", 503, "temporarily unavailable", retry_after=1)
        doc = empty_completed()
        if interruption == "changed_node" and clock.now >= 1010:
            doc["mapping"]["empty"]["message"]["id"] = "new-empty"
        return doc
    monkeypatch.setattr(instance, "_get_conversation", get)
    monkeypatch.setattr(instance, "_query_backend_tasks", lambda **kw:
                        [{"status": "pending"}] if interruption == "task" and clock.now == 1010 else [])
    with pytest.raises(ImagePollTimeoutError):
        instance._poll_image_results("synthetic", timeout_secs=24)


@pytest.mark.parametrize("kind", ["quota", "empty"])
def test_terminal_poll_rotates_and_returns_saved_image(backend, retry_flow, monkeypatch, tmp_path, kind):
    import base64
    from io import BytesIO
    from PIL import Image
    from services.protocol import conversation as protocol
    from test_image_account_selection import _account

    instance, _clock = backend
    retry_flow.service._accounts.update({str(i): _account(str(i), quota=8, unknown=False) for i in range(2)})
    doc = empty_completed() if kind == "empty" else {
        "mapping": {"tool": {"message": message("tool", "text", DEFERRED_QUOTA)}}}
    monkeypatch.setattr(instance, "_get_conversation", lambda *a, **kw: doc)
    monkeypatch.setattr(instance, "_query_backend_tasks", lambda **kw: [])
    png = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(png, format="PNG")
    data = png.getvalue()
    saved = tmp_path / "result.png"
    def save(content, *a, **kw):
        saved.write_bytes(content)
        return "http://localhost/images/result.png"
    monkeypatch.setattr(protocol, "save_image_bytes", save)
    def output(selected, request, index, total):
        if selected.token == "0":
            instance._poll_image_results("synthetic-failed", timeout_secs=120)
            pytest.fail("must not deliver the failed account")
        selected.download_image_bytes = lambda urls: [data]
        yield protocol._image_result_output_from_urls(
            selected, request, "synthetic-success", ["https://example.invalid/output.png"], index, total)
    monkeypatch.setattr(protocol, "stream_image_outputs", output)
    result = protocol._generate_single_image(protocol.ConversationRequest(
        model="gpt-image-2", prompt="synthetic", response_format="b64_json"), 1, 1)[0]
    assert saved.read_bytes() == base64.b64decode(result.data[0]["b64_json"]) == data
    assert result.conversation_id == "synthetic-success"
    first, second = result.image_attempts
    assert first["failure_code"] == ("image_quota_exhausted" if kind == "quota" else "no_image_generated")
    assert first["switched_account"] and second["status"] == "success"
    assert retry_flow.selected == retry_flow.closed == ["0", "1"]
    assert retry_flow.service._image_inflight == {}


def test_account_submetrics_are_visible_without_double_counting():
    from services.request_detail_view import build_request_timeline_presentation
    from services.monitor_view import _project_event
    from services.realtime_monitor_service import RealtimeMonitorService
    from api.monitor_contract import MonitorEventView
    metrics = {"account_wait_ms": 14325, "account_snapshot_check_ms": 1,
               "account_candidate_total_ms": 44, "account_token_maintenance_ms": 14278}
    view = build_request_timeline_presentation(metrics, [], wall_duration_ms=14325)
    assert sum(segment["value_ms"] for segment in view["segments"]) == 14325
    assert {step["key"] for group in view["groups"] for step in group["steps"]} >= set(metrics)
    monitor = RealtimeMonitorService()
    monitor.start("synthetic", endpoint="/v1/images/generations", model="fixture")
    monitor.stage("synthetic", "image_account_lookup", account_candidate_attempts=1, **metrics)
    event = MonitorEventView.model_validate(_project_event(monitor._events[-1]))
    for key, value in metrics.items():
        assert getattr(event, key) == value
    assert event.account_candidate_attempts == 1
