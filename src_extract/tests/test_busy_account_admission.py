"""First-call slot pressure is not the same as an empty/unusable account pool."""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from services.account_service import ImageAccountSelectionError
from test_account_auth_quarantine import jwt, row, service_factory


@pytest.fixture(autouse=True)
def strict(monkeypatch):
    monkeypatch.setenv("CHATGPT2API_STRICT_IMAGE_CREDENTIALS", "1")


def configure(service, monkeypatch, *, busy=.5):
    monkeypatch.setattr(service, "_IMAGE_POOL_WAIT_SECONDS", .025)
    monkeypatch.setattr(service, "_IMAGE_BUSY_WAIT_SECONDS", busy, raising=False)
    monkeypatch.setattr(service, "_refresh_accounts_snapshot_if_stale", lambda **kw: False)


def test_first_request_waits_for_busy_account_and_succeeds_after_short_pool_window(service_factory, monkeypatch):
    token = jwt(7200)
    service = service_factory([row(token)])
    configure(service, monkeypatch)
    first = service.get_available_access_token()
    with ThreadPoolExecutor(1) as executor:
        pending = executor.submit(service.get_available_access_token, deadline_monotonic=time.monotonic()+2)
        time.sleep(.12)  # The old empty-pool window is only .025 seconds.
        service.release_image_slot(first)
        second = pending.result(timeout=1)
    assert second == token
    service.release_image_slot(second)
    assert not service._image_inflight


def test_busy_wait_cannot_outlive_request_deadline(service_factory, monkeypatch):
    service = service_factory([row(jwt(7200))])
    configure(service, monkeypatch, busy=2)
    first = service.get_available_access_token()
    try:
        with pytest.raises(ImageAccountSelectionError) as caught:
            service.get_available_access_token(deadline_monotonic=time.monotonic()+.08)
        assert caught.value.code == "task_interrupted"
        assert sum(service._image_inflight.values()) == 1
    finally:
        service.release_image_slot(first)


def test_busy_wait_is_bounded_even_with_long_request_budget(service_factory, monkeypatch):
    service = service_factory([row(jwt(7200))])
    configure(service, monkeypatch, busy=.08)
    first = service.get_available_access_token()
    began = time.monotonic()
    try:
        with pytest.raises(ImageAccountSelectionError) as caught:
            service.get_available_access_token(deadline_monotonic=began+5)
        assert caught.value.code == "no_available_account"
        assert .06 <= time.monotonic()-began < .6
        metrics = service.get_image_selection_diagnostics()
        assert metrics["account_wait_reason"] == "all_ready_accounts_busy"
        assert metrics["busy_count"] == metrics["ready_count"] == 1
    finally:
        service.release_image_slot(first)


@pytest.mark.parametrize("accounts", [[], [row(jwt(-3600), refresh_token="synthetic-rt")],
                                     [row(jwt(7200), status="限流", quota=0)]])
def test_busy_extension_does_not_wait_on_empty_expired_or_exhausted_pools(service_factory, monkeypatch, accounts):
    service = service_factory(accounts)
    configure(service, monkeypatch, busy=2)
    began = time.monotonic()
    with pytest.raises(ImageAccountSelectionError):
        service.get_available_access_token(deadline_monotonic=began+5)
    assert time.monotonic()-began < .5
    assert not service._image_inflight


def test_saved_busy_diagnostics_keep_zero_counts_and_shard_identity():
    from services.realtime_monitor_service import RealtimeMonitorService
    from services.monitor_view import _project_event
    from api.monitor_contract import MonitorEventView
    monitor = RealtimeMonitorService.__new__(RealtimeMonitorService)
    data = dict(account_wait_reason="all_ready_accounts_busy", account_shard_index=0, account_shard_count=8,
                matched_count=13, ready_count=2, busy_count=2, limited_count=0,
                upload_limited_count=0, available_slot_count=0, selection_loop_count=3,
                selection_wait_ms=2828)
    event = monitor._event("synthetic-busy", "image_local_rejected", {}, data)
    attempts = {}
    monitor._accumulate_attempt_event(attempts, {**event, "index":1, "attempt":1})
    saved = monitor._detail_diagnostic({}, [event])["events"][0]
    view = MonitorEventView.model_validate(_project_event(event))
    for key, value in data.items():
        assert event[key] == saved[key] == getattr(view, key) == value
        assert attempts[(1, 1)]["events"][0][key] == value


def test_busy_deadline_is_not_restarted_by_another_candidate_scan(service_factory, monkeypatch):
    service = service_factory([row(jwt(7200))])
    configure(service, monkeypatch, busy=2)
    first = service.get_available_access_token()
    began = time.monotonic()
    try:
        with pytest.raises(ImageAccountSelectionError):
            service._acquire_next_candidate_token(deadline_monotonic=began+5, busy_wait_deadline=began-.01)
        assert time.monotonic()-began < .2
    finally:
        service.release_image_slot(first)


def test_real_generation_loop_waits_before_first_attempt_and_releases_both_slots(service_factory, monkeypatch):
    from types import SimpleNamespace
    from services.protocol import conversation as protocol
    from services.credential_coordinator import CredentialGate
    token = jwt(7200)
    service = service_factory([row(token)])
    configure(service, monkeypatch)
    monkeypatch.setattr(protocol, "account_service", service)
    monkeypatch.setattr(protocol, "config", SimpleNamespace(
        image_account_retry_enabled=True, image_max_account_attempts=3, image_stream_timeout_secs=90))
    monkeypatch.setattr(protocol, "proxy_settings", SimpleNamespace(
        acquire_image_egress=lambda *a, **kw:0, get_fallback_proxy_reference=lambda:""))
    monkeypatch.setattr(protocol, "_cleanup_image_conversations_after_success", lambda *a:None)
    class Backend:
        def __init__(self, **kw): self.proxy_profile = SimpleNamespace()
        def close(self): pass
    monkeypatch.setattr(protocol, "OpenAIBackendAPI", Backend)
    calls = []
    def output(backend, request, index, total):
        calls.append(1)
        yield protocol.ImageOutput(kind="result", model=request.model, index=index, total=total,
                                   data=[{"url":"/images/synthetic.png"}])
    monkeypatch.setattr(protocol, "stream_image_outputs", output)
    first = service.get_available_access_token()
    request = protocol.ConversationRequest(model="gpt-image-2", deadline_monotonic=time.monotonic()+2)
    with ThreadPoolExecutor(1) as executor:
        pending = executor.submit(protocol._generate_single_image, request, 1, 1)
        time.sleep(.12)
        service.release_image_slot(first)
        outputs = pending.result(timeout=1)
    assert outputs[0].kind == "result"
    assert len(outputs[0].image_attempts) == 1 and calls == [1]
    assert not service._image_inflight
    import json
    with service._credential_coordinator.Session() as session:
        assert all(not json.loads(record.data)["leases"] for record in session.query(CredentialGate).all())
