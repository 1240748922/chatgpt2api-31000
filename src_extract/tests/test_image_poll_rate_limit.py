"""Replay read-side throttling without submitting another image generation."""
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
from services.image_failure import ImageGenerationError, ImagePollTimeoutError
from services.protocol import conversation as protocol
from test_image_account_retries import retry_flow
from test_image_account_selection import _account
from test_image_recovery import backend, document
from utils.helper import UpstreamHTTPError


def limited(retry_after=None):
    return UpstreamHTTPError(
        "/backend-api/conversation/existing", 429,
        {"detail": "Too many requests"}, retry_after=retry_after,
    )


@pytest.mark.parametrize("retry_after", [2, 13, 24, 55])
def test_poll_retries_same_conversation_after_upstream_delay(backend, monkeypatch, retry_after):
    instance, clock = backend
    monkeypatch.setattr(api.random, "uniform", lambda *a: 0)
    calls = []

    def get(conversation_id, timeout_secs):
        calls.append((conversation_id, clock.now, timeout_secs))
        if len(calls) == 1:
            raise limited(retry_after)
        return document(complete=True)

    monkeypatch.setattr(instance, "_get_conversation", get)
    monkeypatch.setattr(instance, "_query_backend_tasks", lambda **kw: pytest.fail("no auxiliary query needed"))
    files, _ = instance._poll_image_results("existing", timeout_secs=60)
    assert files == ["file_0123456789abcdef0123456789abcdef"]
    assert calls == [("existing", 1000, 10), ("existing", 1000 + retry_after, min(10, 60 - retry_after))]


@pytest.mark.parametrize("retry_after, expected_times", [
    (None, [1000, 1002, 1006, 1014, 1030]),
    (0, [1000, 1001, 1002, 1003, 1004]),
])
def test_missing_or_zero_header_has_bounded_non_busy_backoff(backend, monkeypatch, retry_after, expected_times):
    instance, clock = backend
    monkeypatch.setattr(api.random, "uniform", lambda *a: 0)
    times = []

    def get(*a, **kw):
        times.append(clock.now)
        if len(times) < len(expected_times):
            raise limited(retry_after)
        return document(complete=True)

    monkeypatch.setattr(instance, "_get_conversation", get)
    files, _ = instance._poll_image_results("existing", timeout_secs=40)
    assert files
    assert times == expected_times


@pytest.mark.parametrize("retry_after, expected_times", [(3, [1000, 1003, 1006, 1009]), (55, [1000])])
def test_persistent_limit_exhausts_existing_budget_without_early_requery(backend, monkeypatch, retry_after, expected_times):
    instance, clock = backend
    monkeypatch.setattr(api.random, "uniform", lambda *a: 0)
    times = []

    def get(*a, **kw):
        times.append(clock.now)
        raise limited(retry_after)

    monkeypatch.setattr(instance, "_get_conversation", get)
    with pytest.raises(UpstreamHTTPError) as caught:
        instance._poll_image_results("existing", timeout_secs=10)
    assert times == expected_times
    assert clock.now == 1010
    failure = caught.value.failure
    assert failure.code == "upstream_rate_limited"
    assert failure.status_code == 429
    assert failure.retry_after == retry_after
    assert not failure.switch_account and not failure.verify_account
    trace = caught.value.poll_trace
    assert trace[0]["status_code"] == 429
    assert trace[0]["retry_after_secs"] == retry_after
    assert sum(item["retry_wait_ms"] for item in trace) == 10000


def test_recovered_limit_does_not_mask_later_result_timeout(backend, monkeypatch):
    instance, clock = backend
    monkeypatch.setattr(api.random, "uniform", lambda *a: 0)
    times = []

    def get(*a, **kw):
        times.append(clock.now)
        if len(times) == 1:
            raise limited(2)
        return document()

    monkeypatch.setattr(instance, "_get_conversation", get)
    monkeypatch.setattr(instance, "_query_backend_tasks", lambda **kw: [])
    with pytest.raises(ImagePollTimeoutError) as caught:
        instance._poll_image_results("existing", timeout_secs=5)
    assert caught.value.failure.code == "image_poll_timeout"
    assert caught.value.failure.retry_after is None
    assert times == [1000, 1002, 1003, 1004]
    assert clock.now == 1005


@pytest.mark.parametrize("status, body, code", [
    (401, {"detail": "invalid token"}, "auth_invalid"),
    (400, {"error": {"code": "content_policy_violation"}}, "content_policy_violation"),
    (429, {"error": {"code": "image_quota_exhausted"}}, "image_quota_exhausted"),
])
def test_auth_refusal_and_confirmed_quota_are_not_query_throttle_retries(backend, monkeypatch, status, body, code):
    instance, clock = backend

    def get(*a, **kw):
        raise UpstreamHTTPError("/backend-api/conversation/existing", status, body, retry_after=13)

    monkeypatch.setattr(instance, "_get_conversation", get)
    with pytest.raises(UpstreamHTTPError) as caught:
        instance._poll_image_results("existing", timeout_secs=60)
    assert caught.value.failure.code == code
    assert caught.value.poll_attempts == 1
    assert clock.now == 1000


@pytest.mark.parametrize("persistent", [False, True])
def test_submitted_generation_survives_poll_limit_and_releases_slot(retry_flow, monkeypatch, tmp_path, persistent):
    """Real SSE decoder, poller, retry loop and PNG delivery; fake network only."""
    clock = protocol.time
    monkeypatch.setattr(api, "time", clock)
    monkeypatch.setattr(api.random, "uniform", lambda *a: 0)
    monkeypatch.setattr(api, "config", SimpleNamespace(
        image_poll_interval_secs=1, image_poll_initial_wait_secs=0,
        image_check_before_hit_enabled=False, image_settle_enabled=False,
        image_poll_timeout_secs=10 if persistent else 60,
        image_stream_timeout_secs=90,
    ))
    retry_flow.settings.image_poll_timeout_secs = 10 if persistent else 60
    retry_flow.settings.global_system_prompt = ""
    retry_flow.service._accounts = OrderedDict(
        (str(i), _account(str(i), quota=8, unknown=False)) for i in range(2)
    )
    png = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(png, format="PNG")
    content = png.getvalue()
    saved = tmp_path / "result.png"
    submissions, polls = [], []
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

        def start(*a):
            submissions.append(access_token)
            queue = Queue()
            payload = {"conversation_id": "existing", "message": document()["mapping"]["m"]["message"]}
            queue.put(("data: " + json.dumps(payload) + "\n\n").encode())
            queue.put(b"data: [DONE]\n\n")
            queue.put(STREAM_END)
            return SimpleNamespace(queue=queue, close=lambda: None)

        def get(cid, timeout_secs):
            polls.append((cid, clock.now))
            if persistent or len(polls) == 1:
                raise limited(13)
            return document(complete=True)

        instance._start_image_generation = start
        instance._get_conversation = get
        instance._query_backend_tasks = lambda **kw: pytest.fail("no auxiliary query needed")
        instance._get_file_download_url = lambda file_id: "https://example.invalid/result.png"
        instance.download_image_bytes = lambda urls: [content]
        return instance

    def save(data, *a, **kw):
        saved.write_bytes(data)
        return "http://localhost/images/result.png"

    monkeypatch.setattr(protocol, "OpenAIBackendAPI", create)
    monkeypatch.setattr(protocol, "save_image_bytes", save)
    request = protocol.ConversationRequest(model="gpt-image-2", prompt="synthetic",
        response_format="b64_json", deadline_monotonic=clock.now + 120)
    if persistent:
        with pytest.raises(ImageGenerationError) as caught:
            protocol._generate_single_image(request, 1, 1)
        assert caught.value.failure.code == "upstream_rate_limited"
        assert caught.value.failure.retry_after == 13
        assert len(caught.value.image_attempts) == 1
        assert not saved.exists()
        assert polls == [("existing", 1000)]
        assert clock.now == 1010
    else:
        outputs = protocol._generate_single_image(request, 1, 1)
        result = next(output for output in outputs if output.kind == "result")
        assert saved.read_bytes() == base64.b64decode(result.data[0]["b64_json"]) == content
        assert len(result.image_attempts) == 1
        assert result.image_attempts[0]["status"] == "success"
        assert polls == [("existing", 1000), ("existing", 1013)]
    assert submissions == retry_flow.selected == retry_flow.closed == ["0"]
    assert retry_flow.service._image_inflight == {}
