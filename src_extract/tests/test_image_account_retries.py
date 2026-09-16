"""Exercise the generation retry loop and real local account leasing, offline."""
from collections import OrderedDict
from threading import Condition, Lock
from types import SimpleNamespace

import pytest

from services.account_service import AccountService
import services.account_service as accounts_module
from services.image_failure import ImageGenerationError, image_failure
from services.protocol import conversation as protocol
from test_image_account_selection import _account
from test_image_recovery import Clock


@pytest.fixture
def retry_flow(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(protocol, "time", clock)
    monkeypatch.setattr(accounts_module, "time", clock)
    settings = SimpleNamespace(image_account_retry_enabled=True,
                               image_max_account_attempts=4, image_stream_timeout_secs=90)
    monkeypatch.setattr(protocol, "config", settings)
    service = AccountService.__new__(AccountService)
    service._lock = Lock()
    service._image_slot_condition = Condition(service._lock)
    service._accounts = OrderedDict()
    service._image_inflight = {}
    service._token_aliases = {}
    service._image_index = 0
    service._image_shard_count = 1
    service._image_shard_index = 0
    service._IMAGE_POOL_WAIT_SECONDS = 0
    monkeypatch.setattr(service, "_refresh_accounts_snapshot_if_stale", lambda **kw: False)
    monkeypatch.setattr(service, "ensure_access_token", lambda token, **kw: token)
    monkeypatch.setattr(service, "get_account", lambda token, **kw: service._accounts[token])
    selected, closed, outcomes = [], [], []
    def mark(token, success, **kw):
        outcomes.append((token, success))
        service.release_image_slot(token)
    monkeypatch.setattr(service, "mark_image_result", mark)
    monkeypatch.setattr(protocol, "account_service", service)
    monkeypatch.setattr(protocol, "proxy_settings", SimpleNamespace(
        acquire_image_egress=lambda *a, **kw: 0, get_fallback_proxy_reference=lambda: ""))
    monkeypatch.setattr(protocol, "_cleanup_image_conversations_after_success", lambda *a: None)
    class Backend:
        def __init__(self, access_token, **kw):
            selected.append(access_token)
            self.token = access_token
            self.proxy_profile = SimpleNamespace()
        def close(self):
            closed.append(self.token)
    monkeypatch.setattr(protocol, "OpenAIBackendAPI", Backend)
    def run(failures, *, budget=120, as_message=False):
        # Exactly one account per scripted outcome. Exhaustion therefore tests
        # that the selector does not recycle previously tried credentials.
        service._accounts = OrderedDict((str(i), _account(str(i), quota=8, unknown=False))
                                        for i in range(len(failures)))
        def output(backend, request, index, total):
            clock.sleep(0.5)
            code = failures[int(backend.token)]
            if code:
                failure = image_failure(code)
                if not as_message:
                    raise ImageGenerationError(code, failure=failure)
                yield protocol.ImageOutput(kind="message", model=request.model, index=index,
                                           total=total, text=code, failure=failure)
            else:
                yield protocol.ImageOutput(kind="result", model=request.model, index=index,
                                           total=total, data=[{"url": "/images/test.png"}])
        monkeypatch.setattr(protocol, "stream_image_outputs", output)
        return protocol._generate_single_image(protocol.ConversationRequest(
            model="gpt-image-2", images=["reference"],
            deadline_monotonic=clock.now + budget if budget else 0), 1, 1)
    return SimpleNamespace(run=run, service=service, selected=selected, closed=closed,
                           outcomes=outcomes, settings=settings)


@pytest.mark.parametrize("code", ["file_upload_throttled", "image_quota_exhausted", "insufficient_quota"])
@pytest.mark.parametrize("as_message", [False, True])
def test_account_capacity_limits_can_reach_sixth_account(retry_flow, code, as_message):
    outputs = retry_flow.run([code] * 5 + [None], as_message=as_message)
    assert outputs[0].kind == "result"
    attempts = outputs[0].image_attempts
    assert len(attempts) == 6
    assert all(a["switched_account"] for a in attempts[:-1])
    assert retry_flow.selected == retry_flow.closed == [str(i) for i in range(6)]
    assert retry_flow.service._image_inflight == {}


def test_capacity_limits_do_not_consume_normal_error_budget(retry_flow):
    failures = ["file_upload_throttled", "image_quota_exhausted"] * 3
    outputs = retry_flow.run(failures + ["image_tool_error"] * 3 + [None])
    assert outputs[0].kind == "result"
    assert len(outputs[0].image_attempts) == 10


def test_normal_errors_still_stop_at_configured_limit(retry_flow):
    failures = ["file_upload_throttled", "image_tool_error"] * 4 + [None]
    with pytest.raises(ImageGenerationError) as caught:
        retry_flow.run(failures)
    assert len(caught.value.image_attempts) == 8
    assert len(retry_flow.selected) == 8
    assert retry_flow.service._image_inflight == {}


def test_capacity_scan_stops_at_request_deadline(retry_flow):
    with pytest.raises(ImageGenerationError) as caught:
        retry_flow.run(["file_upload_throttled"] * 20, budget=3)
    assert len(caught.value.image_attempts) == 6
    assert retry_flow.service._image_inflight == {}


def test_capacity_scan_exhausts_each_distinct_account_once(retry_flow):
    with pytest.raises(ImageGenerationError) as caught:
        retry_flow.run(["file_upload_throttled"] * 7)
    assert len(caught.value.image_attempts) == 7
    assert retry_flow.selected == retry_flow.closed == [str(i) for i in range(7)]
    assert retry_flow.service._image_inflight == {}


@pytest.mark.parametrize("code", ["file_upload_throttled", "image_quota_exhausted",
                                 "upstream_rate_limited", "content_policy_violation"])
def test_retry_toggle_and_request_level_failures_remain_terminal(retry_flow, code):
    if code in {"file_upload_throttled", "image_quota_exhausted"}:
        retry_flow.settings.image_account_retry_enabled = False
    with pytest.raises(ImageGenerationError):
        retry_flow.run([code, None])
    assert len(retry_flow.selected) == 1
    assert retry_flow.service._image_inflight == {}
