import asyncio
import threading
import time
import pytest
from contextlib import contextmanager

from services import retention_cleanup_service as retention
from services.async_execution import BoundedExecution, ExecutionBusy, ExecutionTimeout


def test_execution_bounds_and_timeout_do_not_release_running_slots():
    async def check():
        pool = BoundedExecution("test-image", 1, 1, 0.05)
        release = threading.Event()
        started = threading.Event()
        ran = []
        def slow():
            started.set()
            release.wait(2)
        first = asyncio.create_task(pool.run(slow, deadline=time.monotonic() + 0.1))
        while not started.is_set():
            await asyncio.sleep(0.001)
        second = asyncio.create_task(pool.run(lambda: ran.append(True)))
        await asyncio.sleep(0.01)
        with pytest.raises(ExecutionBusy):
            await pool.run(lambda: None)
        with pytest.raises(ExecutionTimeout):
            await second
        with pytest.raises(ExecutionTimeout):
            await first
        assert pool.snapshot()["active"] == 1
        assert pool.snapshot()["waiting"] == 1
        with pytest.raises(ExecutionBusy):
            await pool.run(lambda: None)
        release.set()
        for _ in range(100):
            if pool.snapshot()["active"] + pool.snapshot()["waiting"] == 0:
                break
            await asyncio.sleep(0.005)
        assert ran == []
        assert await pool.run(lambda: 42) == 42
        pool.shutdown()
    asyncio.run(check())


def test_cancelled_running_iterator_is_closed_after_worker_finishes():
    async def check():
        pool = BoundedExecution("test-stream", 1, 0)
        release = threading.Event()
        closed = threading.Event()
        started = threading.Event()
        def stream():
            try:
                started.set()
                release.wait(2)
                yield 1
            finally:
                closed.set()
        iterator = stream()
        task = asyncio.create_task(pool.run(next, iterator, discard=lambda _: iterator.close()))
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pool.snapshot()["active"] == 1
        release.set()
        for _ in range(100):
            if closed.is_set():
                break
            await asyncio.sleep(0.005)
        assert closed.is_set()
        pool.shutdown()
    asyncio.run(check())


def test_slow_image_log_does_not_block_health_event_loop(monkeypatch):
    from services.log_service import LoggedCall
    from fastapi import FastAPI
    import httpx
    call = LoggedCall({}, "/v1/images/generations", "gpt-image-2", "fixture")
    entered = threading.Event()
    release = threading.Event()
    def slow_log(*args, **kwargs):
        entered.set()
        release.wait(2)
    monkeypatch.setattr(call, "log", slow_log)
    app = FastAPI()
    @app.get("/version")
    async def version():
        return {"version": "fixture"}
    async def check():
        job = asyncio.create_task(call.run(lambda: {"data": []}))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set()
            assert not job.done(), "blocked loop resumed only after synchronous log completed"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://fixture") as client:
                start=time.monotonic()
                assert (await client.get("/version")).status_code == 200
                assert time.monotonic()-start < 0.1
        finally:
            release.set()
            await job
    asyncio.run(check())


def test_http_disconnect_cancels_waiter():
    from api.ai import run_connected
    from fastapi import HTTPException
    class Request:
        async def is_disconnected(self):
            return True
    cancelled = asyncio.Event()
    class Call:
        async def run(self, handler, payload):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
    async def check():
        with pytest.raises(HTTPException) as error:
            await run_connected(Request(), Call(), None, {})
        assert error.value.status_code == 499
        assert cancelled.is_set()
    asyncio.run(check())


def test_stream_disconnect_closes_full_iterator_chain(monkeypatch):
    from services import log_service as logs
    from utils.helper import image_sse_stream
    closed = threading.Event()
    def outputs():
        try:
            yield {"type": "fixture", "data": []}
            yield {"type": "fixture", "data": []}
        finally:
            closed.set()
    call = logs.LoggedCall({}, "/v1/images/generations", "gpt-image-2", "fixture")
    monkeypatch.setattr(call, "log", lambda *args, **kwargs: None)
    iterator=outputs()
    first=next(iterator)
    stream=logs._closing_sender(image_sse_stream,call.stream(logs._prepend_and_close(first,iterator)))
    async def check():
        async_stream=logs._iterate_image_stream(stream)
        await anext(async_stream)
        await async_stream.aclose()
        assert closed.is_set()
    asyncio.run(check())


def test_continuous_synthetic_image_load_keeps_real_version_route_responsive(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from api import system
    from services import log_service as logs
    from services.config import config
    pool=BoundedExecution("synthetic-image",4,4,0.1)
    monkeypatch.setattr(logs,"_IMAGE_POOL",pool)
    monkeypatch.setattr(config.__class__,"image_request_timeout_secs",property(lambda _: 0.25))
    monkeypatch.setattr(logs.LoggedCall,"log",lambda *a,**kw: None)
    app=FastAPI()
    app.include_router(system.create_router("fixture"))
    samples=[]
    async def load():
        for _ in range(5):
            calls=[logs.LoggedCall({},"/v1/images/generations","gpt-image-2","fixture") for i in range(20)]
            responses=await asyncio.gather(*(c.run(lambda: (time.sleep(0.08) or {"data":[]})) for c in calls))
            assert any(getattr(r,"status_code",200)==503 for r in responses)
    async def check():
        task=asyncio.create_task(load())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://fixture") as client:
            while not task.done():
                start=time.monotonic()
                assert (await client.get("/version")).status_code==200
                samples.append(time.monotonic()-start)
                await asyncio.sleep(0.005)
        await task
        assert len(samples)>20
        assert max(samples)<0.2
        for _ in range(100):
            if pool.snapshot()["active"]+pool.snapshot()["waiting"]==0:
                break
            await asyncio.sleep(0.01)
        assert pool.snapshot()["active"]==pool.snapshot()["waiting"]==0
        pool.shutdown()
    asyncio.run(check())


def test_ingress_limit_rejects_before_body_read_and_releases_on_disconnect():
    from api.admission import GenerationAdmissionMiddleware
    async def check():
        release = asyncio.Event()
        entered = asyncio.Event()
        async def app(scope,receive,send):
            entered.set()
            await release.wait()
        middleware = GenerationAdmissionMiddleware(app,capacity=1)
        async def receive():
            pytest.fail("overload must not read uploaded body")
        sent=[]
        async def send(message):sent.append(message)
        scope={"type":"http","method":"POST","path":"/v1/images/edits"}
        first=asyncio.create_task(middleware(scope,receive,send))
        await entered.wait()
        await middleware(scope,receive,send)
        assert sent[0]["status"]==503
        first.cancel()
        with pytest.raises(asyncio.CancelledError):await first
        assert middleware.active==0
    asyncio.run(check())


def test_image_storage_fallback_cannot_block_version_or_dashboard(monkeypatch):
    from api import system
    from fastapi import FastAPI
    from fastapi.responses import Response
    import httpx
    entered=threading.Event()
    release=threading.Event()
    def blocked_image(path):
        entered.set()
        release.wait(2)
        return Response(b"fixture",media_type="image/png")
    monkeypatch.setattr(system,"get_image_response",blocked_image)
    monkeypatch.setattr(system,"require_admin",lambda _: {})
    # Avoid external state while exercising the real dashboard route/executor.
    from fastapi.responses import JSONResponse
    monkeypatch.setattr(system,"build_dashboard_view",lambda **_: JSONResponse({
        "status":"ok", "healthy":True, "version":"fixture",
    }))
    app=FastAPI()
    app.include_router(system.create_router("fixture"))
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://fixture") as client:
            job=asyncio.create_task(client.get("/images/test.png"))
            try:
                for _ in range(100):
                    if entered.is_set():break
                    await asyncio.sleep(0.005)
                assert entered.is_set()
                assert not job.done(),"file read blocked the event loop"
                start=time.monotonic()
                assert (await client.get("/version")).status_code==200
                assert (await client.get("/api/dashboard")).status_code==200
                assert time.monotonic()-start<0.1
            finally:
                release.set()
                assert (await job).status_code==200
    asyncio.run(check())


class MemoryState:
    def __init__(self):
        self.data = {"next_run_epoch": time.time() - 10, "wake_revision": 0}

    @contextmanager
    def edit(self):
        yield self.data


def test_deferred_cleanup_advances_shared_deadline(monkeypatch):
    repository = MemoryState()
    coordinator = retention.RetentionCleanupCoordinator(repository=repository)
    stop = threading.Event()
    attempts = []

    def deferred(**kwargs):
        attempts.append(time.monotonic())
        return {"deferred": True, "maintenance_load": {"retry_seconds": 30}}

    monkeypatch.setattr(coordinator, "run_automatic", deferred)
    thread = threading.Thread(target=coordinator._scheduler_leader_loop, args=(stop,))
    thread.start()
    try:
        time.sleep(0.25)
        assert len(attempts) == 1
        assert repository.data["next_run_epoch"] > time.time() + 25
    finally:
        stop.set()
        thread.join(2)
    assert not thread.is_alive()


def test_retention_retry_preserves_newer_settings_notification():
    repository = MemoryState()
    coordinator = retention.RetentionCleanupCoordinator(repository=repository)
    coordinator.notify_retention_change()
    scheduled = repository.data["next_run_epoch"]
    assert coordinator._schedule_after_cleanup(0, delay_seconds=30) == scheduled


def test_retention_retry_without_repository():
    coordinator = retention.RetentionCleanupCoordinator()
    before = time.time()
    assert before + 5 <= coordinator._schedule_after_cleanup(0, delay_seconds=5) <= time.time() + 5


def test_cancelled_stream_is_not_logged_as_success(monkeypatch):
    from services.log_service import LoggedCall
    records=[]
    call=LoggedCall({},"/v1/images/generations","gpt-image-2","fixture")
    monkeypatch.setattr(call,"log",lambda *a,**kw: records.append(kw))
    stream=call.stream(iter([{"data":[]},{"data":[]}]))
    next(stream)
    stream.close()
    assert len(records)==1
    assert records[0]['status']=='failed'
    assert records[0]['error']=='stream_closed_before_completion'


def test_gateway_excludes_admin_without_changing_singleton_or_import_routes():
    from pathlib import Path
    root=Path(__file__).resolve().parents[2]
    nginx=(root/'nginx.conf').read_text()
    pool=nginx.split('upstream chatgpt2api_pool {',1)[1].split('}',1)[0]
    assert 'server app0:' not in pool
    assert all(f'server app{i}:80 resolve' in pool for i in range(1,8))
    assert 'server app0:80 resolve;' in nginx
    assert 'location ^~ /api/account-import-jobs' in nginx
    assert 'proxy_pass http://chatgpt2api_importer;' in nginx
    assert 'location /v1/image-tasks' in nginx
    assert 'location @image_backend' in nginx
    compose=(root/'docker-compose.yml').read_text()
    assert 'CHATGPT2API_ACCOUNT_SHARD_COUNT: 8' in compose
    assert 'app0: {<<: *app, environment: {<<: *env, CHATGPT2API_ACCOUNT_SHARD_INDEX: 0}}' in compose


def test_watchdog_recovers_only_continuous_stall_without_sensitive_stack(caplog):
    from services.runtime_health import EventLoopWatchdog
    import logging
    class StopAfter:
        def __init__(self,count):self.count=count
        def wait(self,_):
            self.count-=1
            return self.count<0
    exits=[]
    watchdog=EventLoopWatchdog(threshold=5,report_interval=0,exit_after=180,exit_func=exits.append)
    watchdog._stop=StopAfter(1)
    watchdog._beat=time.monotonic()-10
    with caplog.at_level(logging.ERROR):watchdog._observe()
    assert exits==[]
    assert 'event_loop_stalled' in caplog.text
    watchdog._stop=StopAfter(1)
    watchdog._beat=time.monotonic()-181
    watchdog._observe()
    assert exits==[70]
    assert 'event_loop_unresponsive_exit' in caplog.text
    watchdog._stop=StopAfter(1)
    watchdog._beat=time.monotonic()
    watchdog._observe()
    assert exits==[70]
