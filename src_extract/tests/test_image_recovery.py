from types import SimpleNamespace

import pytest

from services import openai_backend_api as api
from services.protocol import conversation as protocol
from services.image_failure import ImageFailureError, ImagePollTimeoutError, image_failure


class Clock:
    def __init__(self):
        self.now = 1000.0
    def monotonic(self):
        return self.now
    def perf_counter(self):
        return self.now
    def time(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


def document(*, complete=False, failure=False):
    content = {"content_type": "code", "text": '{"size":"1024x1024","prompt":"a tree"}'}
    if complete:
        content = {"content_type": "multimodal_text", "parts": [
            {"content_type": "image_asset_pointer", "asset_pointer": "file-service://file_0123456789abcdef0123456789abcdef"},
        ]}
    message = {"author": {"role": "tool" if complete else "assistant"}, "content": content, "create_time": 1}
    if failure:
        message["metadata"] = {"error": {"code": "content_policy_violation"}}
    return {"mapping": {"m": {"message": message}}}


@pytest.fixture
def backend(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(api, "time", clock)
    monkeypatch.setattr(api, "config", SimpleNamespace(
        image_poll_interval_secs=1, image_poll_initial_wait_secs=0,
        image_check_before_hit_enabled=False, image_settle_enabled=False,
    ))
    instance = api.OpenAIBackendAPI.__new__(api.OpenAIBackendAPI)
    monkeypatch.setattr(instance, "_reset_image_result_timing", lambda: None)
    monkeypatch.setattr(instance, "_add_image_result_timing", lambda *args: None)
    monkeypatch.setattr(instance, "_sleep_for_image_poll", clock.sleep)
    return instance, clock


def test_completed_image_is_found_before_slow_auxiliary_task_probe(backend, monkeypatch):
    instance, clock = backend
    calls = []
    def get(conversation_id, timeout_secs):
        calls.append((conversation_id, timeout_secs))
        return document(complete=True)
    monkeypatch.setattr(instance, "_get_conversation", get)
    monkeypatch.setattr(instance, "_query_backend_tasks", lambda **kwargs: pytest.fail("queried tasks after image was ready"))
    files, sediments = instance._poll_image_results("existing-conversation", timeout_secs=2)
    assert files == ["file_0123456789abcdef0123456789abcdef"]
    assert calls == [("existing-conversation", 2)]
    assert clock.now == 1000


def test_poll_keeps_generation_arguments_pending_until_image_arrives(backend, monkeypatch):
    instance, clock = backend
    docs = iter([document(), document(complete=True)])
    monkeypatch.setattr(instance, "_get_conversation", lambda *a, **kw: next(docs))
    monkeypatch.setattr(instance, "_query_backend_tasks", lambda **kw: [])
    files, _ = instance._poll_image_results("existing-conversation", timeout_secs=3)
    assert files == ["file_0123456789abcdef0123456789abcdef"]
    assert clock.now == 1001


def test_poll_budget_bounds_every_http_query(backend, monkeypatch):
    instance, clock = backend
    timeouts = []
    def get(*args, timeout_secs):
        timeouts.append(timeout_secs)
        clock.sleep(timeout_secs)
        return {"mapping": {}}
    monkeypatch.setattr(instance, "_get_conversation", get)
    monkeypatch.setattr(instance, "_query_backend_tasks", lambda **kw: pytest.fail("task query exceeded poll budget"))
    with pytest.raises(ImagePollTimeoutError) as exc:
        instance._poll_image_results("existing-conversation", timeout_secs=2)
    assert timeouts == [2]
    assert clock.now == 1002
    assert exc.value.poll_attempts == 1
    assert exc.value.conversation_id == "existing-conversation"


def test_stream_recovery_polls_same_conversation_within_remaining_deadline(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(protocol, "time", clock)
    monkeypatch.setattr(protocol, "config", SimpleNamespace(image_stream_timeout_secs=60, image_poll_timeout_secs=120))
    calls = []
    def get(cid, timeout_secs):
        assert timeout_secs <= 7
        clock.sleep(1)
        return {"mapping": {}}
    def resolve(cid, files, sediments, **kwargs):
        calls.append((cid, kwargs))
        return ["https://example.invalid/result.png"]
    instance = SimpleNamespace(
        _get_conversation=get, _conversation_poll_snapshot=lambda _: ({}, ""),
        _extract_image_tool_records=lambda _: [], _query_backend_tasks=lambda **kw: [],
        resolve_conversation_image_urls=resolve,
    )
    output = object()
    monkeypatch.setattr(protocol, "_image_result_output_from_urls", lambda *a, **kw: output)
    result = protocol._recover_after_image_stream_timeout(
        instance, protocol.ConversationRequest(deadline_monotonic=1012),
        {"conversation_id": "existing-conversation"}, TimeoutError("SSE closed"), 1, 1, 950,
    )
    assert result is output
    assert calls == [("existing-conversation", {"poll": True, "poll_timeout_secs": 6})]


def test_expired_request_does_not_probe_or_poll_again(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(protocol, "time", clock)
    monkeypatch.setattr(protocol, "config", SimpleNamespace(image_stream_timeout_secs=60, image_poll_timeout_secs=120))
    # No backend methods are provided: any network probe is an error.
    with pytest.raises(protocol.ImageGenerationError) as exc:
        protocol._recover_after_image_stream_timeout(
            SimpleNamespace(), protocol.ConversationRequest(deadline_monotonic=999),
            {"conversation_id": "existing-conversation"}, TimeoutError("SSE closed"), 1, 1, 950,
        )
    assert exc.value.failure.code == "image_stream_timeout"


def test_explicit_generic_task_failure_ends_poll_immediately(backend, monkeypatch):
    instance, clock = backend
    monkeypatch.setattr(instance, "_get_conversation", lambda *a, **kw: document())
    monkeypatch.setattr(instance, "_query_backend_tasks", lambda **kw: [{
        "image_gen_message": {
            "author": {"role": "assistant"}, "status": "failed",
            "content": {"content_type": "text", "parts": []},
        },
    }])
    with pytest.raises(ImageFailureError) as caught:
        instance._poll_image_results("existing-conversation", timeout_secs=120)
    assert caught.value.failure.code == "upstream_error"
    assert caught.value.poll_attempts == 1
    assert caught.value.poll_trace[0]["tasks"][0]["message_status"] == "failed"
    assert clock.now == 1000


def test_running_tasks_keep_polling_and_trace_is_bounded(backend, monkeypatch):
    instance, clock = backend
    monkeypatch.setattr(instance, "_get_conversation", lambda *a, **kw: document())
    monkeypatch.setattr(instance, "_query_backend_tasks", lambda **kw: [{
        "status": "running", "access_token": "must-not-be-logged",
        "image_gen_message": {
            "author": {"role": "assistant"}, "status": "in_progress",
            "content": {"content_type": "code", "text": '{"prompt":"a tree","size":"1024x1024"}'},
        },
    }])
    with pytest.raises(ImagePollTimeoutError) as caught:
        instance._poll_image_results("existing-conversation", timeout_secs=25)
    trace = caught.value.poll_trace
    assert caught.value.poll_attempts == 25
    assert len(trace) == 16
    assert trace[0]["attempt"] == 1
    assert trace[-1]["attempt"] == 25
    assert trace[-1]["tasks"][0]["status"] == "running"
    assert "must-not-be-logged" not in str(trace)
    assert clock.now == 1025
