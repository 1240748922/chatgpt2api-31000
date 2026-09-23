"""Exclusive page inventory and a real curl connection handed between threads."""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
from types import SimpleNamespace
import time

import pytest
from curl_cffi import requests

from services.page_prewarm_pool import PagePrewarmPool
from services.proxy_service import ProxyRuntimeProfile


class FakeBackend:
    def __init__(self, token="synthetic-at", **account):
        self.access_token = token
        self.account = dict(access_token=token, refresh_token="synthetic-rt", **account)
        self.proxy_profile = ProxyRuntimeProfile()
        self.closed = 0

    def close(self):
        self.closed += 1


def test_inventory_single_consumer_and_miss_does_not_wait():
    pool, backend = PagePrewarmPool(1, 30), FakeBackend()
    pool.put(backend)
    with ThreadPoolExecutor(8) as executor:
        results = list(executor.map(lambda _: pool.take(backend.account, backend.proxy_profile)[0], range(8)))
    assert results.count(backend) == 1 and results.count(None) == 7
    assert pool.status()["hit"] == 1 and pool.status()["miss"] == 7
    assert not pool.candidates()


@pytest.mark.parametrize("change", ["rt", "fp", "proxy", "clearance", "expired"])
def test_mismatch_and_expiry_close_without_reuse(change):
    pool, backend = PagePrewarmPool(1, 30), FakeBackend()
    pool.put(backend)
    account, profile = dict(backend.account), backend.proxy_profile
    if change == "rt":
        account["refresh_token"] = "rotated-rt"
    if change == "fp":
        account["fp"] = {"user-agent": "new-agent"}
    if change == "proxy":
        profile = replace(profile, proxy_url="http://127.0.0.1:9")
    if change == "clearance":
        profile = replace(profile, clearance={"mode": "manual"})
    if change == "expired":
        key, value, stamp = pool.entries[backend.access_token]
        pool.entries[backend.access_token] = (key, value, stamp - 100)
    assert pool.take(account, profile) == (None, 0)
    assert backend.closed == 1


def test_egress_reservation_is_not_a_fingerprint_change():
    pool, backend = PagePrewarmPool(1, 30), FakeBackend()
    pool.put(backend)
    profile = replace(backend.proxy_profile, image_egress_reserved=True, image_egress_wait_ms=30)
    assert pool.take(backend.account, profile)[0] is backend


def test_capacity_shutdown_and_late_build_close_exactly_once():
    pool = PagePrewarmPool(1, 30)
    a, b, late = FakeBackend("a"), FakeBackend("b"), FakeBackend("late")
    assert pool.put(a)
    assert not pool.put(b)
    pool.stop()
    assert not pool.put(late)
    assert (a.closed, b.closed, late.closed) == (1, 1, 1)


def test_curl_same_keepalive_connection_moves_to_another_thread():
    ports = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_GET(self):
            ports.append(self.client_address[1])
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = requests.Session(use_thread_local_curl=False)
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        with ThreadPoolExecutor(1) as builder:
            assert builder.submit(session.get, url, timeout=2).result().status_code == 200
        # Different OS thread, same owned handle: no extra TCP connection.
        assert session.get(url, timeout=2).status_code == 200
        assert len(ports) == 2 and ports[0] == ports[1]
    finally:
        session.close()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_transfer_only_page_state_and_bootstrap_is_skipped_once(monkeypatch):
    from services import openai_backend_api as module
    from services.page_prewarm_pool import page_prewarm_pool
    account = dict(access_token="synthetic-at", refresh_token="synthetic-rt")
    monkeypatch.setattr(module.account_service, "get_account", lambda *a, **kw: dict(account))
    monkeypatch.setattr(module.proxy_settings, "get_profile", lambda *a, **kw: ProxyRuntimeProfile())
    pool = PagePrewarmPool(1, 30)
    monkeypatch.setattr(page_prewarm_pool, "take", pool.take)
    warm = module.OpenAIBackendAPI(access_token="synthetic-at", transferable_session=True)
    warm.pow_script_sources = ["synthetic-page-sdk"]
    warm.pow_data_build = "synthetic-build"
    warm.progress_callback = lambda _: pytest.fail("old callback transferred")
    session = warm.session
    fp = warm.fp
    pool.put(warm)
    cold_calls = []
    with module.OpenAIBackendAPI(access_token="synthetic-at", use_page_prewarm=True) as active:
        assert active.session is session
        assert active.fp == fp and active.pow_data_build == "synthetic-build"
        assert active.progress_callback is None
        assert active.deadline_monotonic is None
        assert warm._closed and warm.session is None
        monkeypatch.setattr(active, "_bootstrap", lambda **kw: cold_calls.append(1))
        active._bootstrap_image()
        assert cold_calls == []
        active._bootstrap_image()
        assert cold_calls == [1]



def test_hit_log_is_a_flag_not_fake_elapsed_time(monkeypatch):
    from services.realtime_monitor_service import RealtimeMonitorService
    from services.monitor_view import _project_event
    from services.request_detail_view import build_request_timeline_presentation
    from api.monitor_contract import MonitorEventView
    monitor = RealtimeMonitorService()
    monitor.start("warm", endpoint="/v1/images/edits", model="fixture")
    monitor.stage("warm", "image_getting_token", index=1, total=1, attempt=1,
                  page_prewarm_hit=1, page_prewarm_age_ms=30000, input_prepare_ms=2000)
    event = list(monitor._events)[-1]
    view = MonitorEventView.model_validate(_project_event(event))
    assert view.page_prewarm_hit == 1
    assert "请求前预热命中" in view.detail_text
    assert "1ms" not in view.timing_text
    diagnostic = monitor._detail_diagnostic({}, [event])
    assert diagnostic["events"][0]["page_prewarm_hit"] == 1
    timeline = build_request_timeline_presentation({"input_prepare_ms": 2000, "page_prewarm_age_ms": 30000}, [], wall_duration_ms=2000)
    assert sum(segment["value_ms"] for segment in timeline["segments"]) == 2000


def test_public_page_retry_after_defers_entire_local_refill_pool():
    pool = PagePrewarmPool(1, 30)
    pool.defer_failure(SimpleNamespace(retry_after=900))
    assert pool.cooldown_until - time.monotonic() > 895
    assert pool.status()["failed"] == 1
    assert pool.status()["cooldown_seconds"] >= 895
    pool.defer_failure(SimpleNamespace(retry_after=1))
    assert pool.cooldown_until - time.monotonic() > 895


def test_inventory_failure_preserves_cold_session(monkeypatch):
    from services import openai_backend_api as module
    from services.page_prewarm_pool import page_prewarm_pool
    monkeypatch.setattr(module.account_service, "get_account", lambda *a, **kw: {"access_token":"synthetic"})
    monkeypatch.setattr(module.proxy_settings, "get_profile", lambda *a, **kw: ProxyRuntimeProfile())
    def failure(*a):
        raise RuntimeError("synthetic inventory failure")
    monkeypatch.setattr(page_prewarm_pool, "take", failure)
    with module.OpenAIBackendAPI(access_token="synthetic") as backend:
        session = backend.session
        backend.adopt_page_prewarm()
        assert backend.session is session and not backend._page_prewarmed
        assert backend._page_prewarm_metrics["page_prewarm_hit"] == 0
