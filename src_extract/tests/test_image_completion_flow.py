"""Replay the premature SSE close captured in all-app-6h.log.

The content is synthetic; the completion patch's paths and boolean values
match captured messages that finished with end_turn=false.
"""
import base64
import json
from io import BytesIO
from queue import Queue
from types import SimpleNamespace

import pytest
from PIL import Image
from curl_cffi.requests.models import STREAM_END
from curl_cffi import CurlOpt

from services import openai_backend_api as api
from services.protocol import conversation as protocol
from test_image_recovery import backend, document
from utils.helper import UpstreamHTTPError


FILE_ID = "file_000000000123456789abcdef01234567"
MESSAGE_COMPLETE = {"p": "", "o": "patch", "v": [
    {"p": "/message/status", "o": "replace", "v": "finished_successfully"},
    {"p": "/message/end_turn", "o": "replace", "v": False},
    {"p": "/message/metadata", "o": "append", "v": {
        "is_complete": True, "finish_details": {"type": "stop", "stop_tokens": [200012]},
    }},
]}


def test_message_complete_is_not_image_turn_complete():
    assert not api.OpenAIBackendAPI._is_image_stream_terminal_payload(json.dumps(MESSAGE_COMPLETE))


def test_image_after_intermediate_completion_reaches_result_and_storage(monkeypatch, tmp_path):
    instance = api.OpenAIBackendAPI.__new__(api.OpenAIBackendAPI)
    instance.access_token = "test-only"
    instance.deadline_monotonic = None
    instance.progress_callback = None
    instance._http_timings = {}
    instance._closed = False
    monkeypatch.setattr(instance, "_bootstrap", lambda **kw: None)
    monkeypatch.setattr(instance, "_get_chat_requirements", lambda **kw: None)
    monkeypatch.setattr(instance, "_prepare_image_conversation", lambda *a: "")
    payloads = [
        {"conversation_id": "current-conversation", "message": {
            "id": "arguments", "author": {"role": "assistant"},
            "content": {"content_type": "code", "text": '{"size":"1024x1024","prompt":"a tree"}'},
            "status": "in_progress", "end_turn": False,
        }},
        MESSAGE_COMPLETE,
        {"message": {
            "id": "image-output", "author": {"role": "tool"},
            "metadata": {"async_task_type": "image_gen"},
            "content": {"content_type": "multimodal_text", "parts": [
                {"content_type": "image_asset_pointer", "asset_pointer": "file-service://" + FILE_ID},
            ]},
            "status": "finished_successfully", "end_turn": False,
        }},
        "[DONE]",
    ]
    stream_queue = Queue()
    for payload in payloads:
        encoded = ("data: " + (payload if isinstance(payload, str) else json.dumps(payload)) + "\n\n").encode()
        # Exercise the actual SSE decoder, including a fragmented data line.
        stream_queue.put(encoded[:19])
        stream_queue.put(encoded[19:])
    stream_queue.put(STREAM_END)
    closed = []
    response = SimpleNamespace(queue=stream_queue, close=lambda: closed.append(True))
    monkeypatch.setattr(instance, "_start_image_generation", lambda *a: response)
    monkeypatch.setattr(instance, "_poll_image_results", lambda *a, **kw: pytest.fail("image was already in SSE"))
    def resolve(cid, files, sediments, **kw):
        assert cid == "current-conversation"
        assert files == [FILE_ID]
        return ["https://example.invalid/generated.png"]
    monkeypatch.setattr(instance, "resolve_conversation_image_urls", resolve)
    png = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(png, format="PNG")
    png_bytes = png.getvalue()
    monkeypatch.setattr(instance, "download_image_bytes", lambda urls: [png_bytes])
    path = tmp_path / "generated.png"
    def save(content, *a, **kw):
        path.write_bytes(content)
        return "http://localhost/images/generated.png"
    monkeypatch.setattr(protocol, "save_image_bytes", save)
    outputs = list(protocol.stream_image_outputs(instance, protocol.ConversationRequest(
        model="gpt-image-2", prompt="a tree", response_format="b64_json",
    )))
    result = next(output for output in outputs if output.kind == "result")
    assert path.read_bytes() == png_bytes
    assert base64.b64decode(result.data[0]["b64_json"]) == png_bytes
    assert result.data[0]["url"] == "http://localhost/images/generated.png"
    assert result.data[0]["width"] == 8
    assert closed


def test_task_image_is_used_while_conversation_is_still_stale(backend, monkeypatch):
    instance, clock = backend
    monkeypatch.setattr(instance, "_get_conversation", lambda *a, **kw: document())
    image_message = document(complete=True)["mapping"]["m"]["message"]
    monkeypatch.setattr(instance, "_query_backend_tasks", lambda **kw: [{
        "conversation_id": "current-conversation", "image_gen_message": image_message,
    }])
    files, _ = instance._poll_image_results("current-conversation", timeout_secs=3)
    assert files == ["file_0123456789abcdef0123456789abcdef"]
    assert clock.now == 1000


@pytest.mark.parametrize("end_turn", [False, None, True])
def test_full_message_requires_explicit_end_turn(end_turn):
    message = {"status": "finished_successfully", "author": {"role": "assistant"},
               "metadata": {"is_complete": True}, "content": {"content_type": "reasoning_recap"}}
    if end_turn is not None:
        message["end_turn"] = end_turn
    assert api.OpenAIBackendAPI._is_image_stream_terminal_payload(json.dumps({"message": message})) is (end_turn is True)


def test_task_input_attachments_are_not_returned_as_generated_images(backend, monkeypatch):
    instance, _ = backend
    docs = iter([document(), document(complete=True)])
    monkeypatch.setattr(instance, "_get_conversation", lambda *a, **kw: next(docs))
    monkeypatch.setattr(instance, "_query_backend_tasks", lambda **kw: [{"image_gen_message": {
        "author": {"role": "user"},
        "content": {"content_type": "multimodal_text", "parts": [{"asset_pointer": "file-service://input-file"}]},
    }}])
    files, _ = instance._poll_image_results("current-conversation", timeout_secs=3)
    assert files == ["file_0123456789abcdef0123456789abcdef"]


def test_bootstrap_recovers_with_fresh_connection_and_keeps_session_options(backend, monkeypatch):
    instance, clock = backend
    instance.deadline_monotonic = None
    instance.session = SimpleNamespace(curl_options={CurlOpt.FRESH_CONNECT: 0})
    calls = []
    def bootstrap(*, timeout_secs):
        calls.append(timeout_secs)
        if len(calls) == 1:
            clock.sleep(timeout_secs)
            raise api.requests.exceptions.Timeout("no response")
        assert instance.session.curl_options[CurlOpt.FRESH_CONNECT] == 1
    monkeypatch.setattr(instance, "_bootstrap", bootstrap)
    instance._bootstrap_image()
    assert calls == [10, 10]
    assert clock.now == 1010
    assert instance.session.curl_options == {CurlOpt.FRESH_CONNECT: 0}


@pytest.mark.parametrize("deadline,expected", [(None, [10, 10]), (1003, [3])])
def test_bootstrap_retries_are_bounded_by_phase_and_request_deadlines(backend, monkeypatch, deadline, expected):
    instance, clock = backend
    instance.deadline_monotonic = deadline
    instance.session = SimpleNamespace(curl_options={})
    calls = []
    def bootstrap(*, timeout_secs):
        calls.append(timeout_secs)
        clock.sleep(timeout_secs)
        raise api.requests.exceptions.Timeout("still no response")
    monkeypatch.setattr(instance, "_bootstrap", bootstrap)
    with pytest.raises(api.requests.exceptions.Timeout):
        instance._bootstrap_image()
    assert calls == expected
    assert clock.now == 1000 + sum(expected)
    assert instance.session.curl_options == {}


@pytest.mark.parametrize("status", [401, 403, 429])
def test_bootstrap_does_not_retry_auth_or_rate_limit_failures(backend, monkeypatch, status):
    instance, _ = backend
    instance.deadline_monotonic = None
    calls = []
    def bootstrap(*, timeout_secs):
        calls.append(timeout_secs)
        raise UpstreamHTTPError("bootstrap", status, {}, credential_scope="public")
    monkeypatch.setattr(instance, "_bootstrap", bootstrap)
    with pytest.raises(UpstreamHTTPError):
        instance._bootstrap_image()
    assert len(calls) == 1
