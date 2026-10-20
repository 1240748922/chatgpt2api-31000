import asyncio
import json
import threading
import time
import pytest

from services import log_service as logs
from services.async_execution import BoundedExecution
from services.realtime_monitor_service import RealtimeMonitorService


@pytest.mark.parametrize('queue', [0, 1])
def test_log_pool_saturation_still_finishes_monitor(monkeypatch, queue):
    async def check():
        pool = BoundedExecution('review-log', 1, queue, 0.05)
        entered = threading.Event()
        release = threading.Event()
        monitor = RealtimeMonitorService()
        stored = []
        monkeypatch.setattr(logs, '_LOG_POOL', pool)
        monkeypatch.setattr(logs, 'realtime_monitor_service', monitor)
        monkeypatch.setattr(logs.log_service, 'add', lambda *a, **kw: stored.append(a))
        def blocked():
            entered.set()
            release.wait(3)
        holder = asyncio.create_task(pool.run(blocked))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            call = logs.LoggedCall({}, '/v1/images/generations', 'gpt-image-2', 'synthetic')
            response = await call.run(lambda: {'data': [{'url': '/synthetic.png'}]})
            observed = {'response': response, 'monitor': monitor.operations_snapshot(), 'stored_logs': len(stored)}
            release.set()
            await holder
            await asyncio.sleep(0.03)
            observed['monitor_after_pool_recovers'] = monitor.operations_snapshot()
            print('LOG_POOL_FULL ' + json.dumps(observed))
            assert response['data']
            assert monitor.operations_snapshot()['active_requests'] == 0
            assert stored == []
            assert len(monitor._completed) == 1
            assert monitor._completed[0]['status'] == 'success'
        finally:
            release.set()
            await holder
            pool.shutdown()
    asyncio.run(check())


@pytest.mark.parametrize('endpoint', ['/v1/images/generations', '/v1/chat/completions', '/v1/responses'])
def test_stream_deadline_sends_structured_sse_error(monkeypatch, endpoint):
    from services.config import config
    async def check():
        pool = BoundedExecution('review-stream', 1, 1, 0.2)
        release = threading.Event()
        closed = threading.Event()
        records = []
        sent = []
        monkeypatch.setattr(logs, '_IMAGE_POOL', pool)
        monkeypatch.setattr(config.__class__, 'image_request_timeout_secs', property(lambda _: 0.08))
        monitor = RealtimeMonitorService()
        monkeypatch.setattr(logs, 'realtime_monitor_service', monitor)
        monkeypatch.setattr(logs.log_service, 'add', lambda *a, **kw: records.append(a[-1]))
        call = logs.LoggedCall({}, endpoint, 'gpt-image-2', 'synthetic')
        def outputs():
            try:
                yield {'type': 'image_generation.in_progress'}
                release.wait(3)
                yield {'type': 'image_generation.completed', 'data': []}
            finally:
                closed.set()
        async def receive():
            await asyncio.Future()
        async def send(message):
            sent.append(message)
        error = None
        try:
            response = await call.run(outputs)
            try:
                await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
            except Exception as exc:
                error = type(exc).__name__ + ': ' + str(exc)
                if exc.__context__ is not None:
                    error += '; context=' + type(exc.__context__).__name__ + ': ' + str(exc.__context__)
            payload = b''.join(item.get('body', b'') for item in sent).decode()
            print('STREAM_TIMEOUT ' + json.dumps({'http_status': sent[0].get('status'), 'body': payload, 'exception': error}))
            assert error is None
            assert 'image_execution_timeout' in payload
            if endpoint.startswith('/v1/images'):
                assert 'event: error' in payload
            else:
                assert payload.endswith('data: [DONE]\n\n')
            assert len(records) == 1
            assert records[0]['status_code'] == 504
            assert records[0]['failure_code'] == 'image_execution_timeout'
            assert monitor.operations_snapshot()['active_requests'] == 0
        finally:
            release.set()
            for _ in range(100):
                if closed.is_set() and pool.snapshot()['active'] == 0:
                    break
                await asyncio.sleep(0.005)
            pool.shutdown()
            assert closed.is_set()
            assert len(records) == 1, 'late worker closure must not overwrite timeout'
    asyncio.run(check())


@pytest.mark.parametrize('streaming', [False, True])
def test_initial_execution_timeout_keeps_diagnostic_fields(monkeypatch, streaming):
    from services.config import config
    async def check():
        pool = BoundedExecution('initial-timeout', 1, 0, 0.5)
        release = threading.Event()
        records = []
        monkeypatch.setattr(logs, '_IMAGE_POOL', pool)
        monkeypatch.setattr(config.__class__, 'image_request_timeout_secs', property(lambda _: 0.08))
        monkeypatch.setattr(logs.log_service, 'add', lambda *a, **kw: records.append(a[-1]))
        def blocked():
            release.wait(3)
            return {'data': []}
        def iterator():
            yield blocked()
        call = logs.LoggedCall({}, '/v1/images/generations', 'gpt-image-2', 'synthetic')
        try:
            response = await call.run(iterator if streaming else blocked)
            assert response.status_code == 504
            assert json.loads(response.body)['error']['code'] == 'image_execution_timeout'
            assert len(records) == 1
            assert records[0]['status_code'] == 504
            assert records[0]['failure_code'] == 'image_execution_timeout'
            assert records[0]['failure_account_failure'] is False
            assert records[0]['failure_retryable'] is False
            assert pool.snapshot()['active'] == 1
        finally:
            release.set()
            for _ in range(100):
                if pool.snapshot()['active'] == 0:
                    break
                await asyncio.sleep(0.005)
            pool.shutdown()
    asyncio.run(check())


def test_stream_busy_reports_error_once_and_keeps_result_metadata(monkeypatch):
    async def check():
        pool = BoundedExecution('busy-stream', 1, 0, 0.5)
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        records = []
        monkeypatch.setattr(logs, '_IMAGE_POOL', pool)
        monkeypatch.setattr(logs.log_service, 'add', lambda *a, **kw: records.append(a[-1]))
        def output():
            try:
                yield {'type': 'image_generation.completed',
                       'data': [{'url': '/images/synthetic.png'}],
                       '_account_email': 'synthetic@example.test',
                       '_conversation_id': 'synthetic-conversation'}
                yield {'type': 'image_generation.completed', 'data': []}
            finally:
                closed.set()
        def blocked():
            entered.set()
            release.wait(3)
        call = logs.LoggedCall({}, '/v1/images/generations', 'gpt-image-2', 'synthetic')
        response = await call.run(output)
        iterator = response.body_iterator
        assert (await anext(iterator)).startswith('event:')
        assert '/images/synthetic.png' in await anext(iterator)
        holder = asyncio.create_task(pool.run(blocked))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            chunks = [chunk async for chunk in iterator]
            assert len(chunks) == 1
            assert 'event: error' in chunks[0]
            assert 'image_execution_busy' in chunks[0]
            assert closed.is_set()
            assert len(records) == 1
            assert records[0]['status_code'] == 503
            assert records[0]['failure_code'] == 'image_execution_busy'
            assert records[0]['account_email'] == 'synthetic@example.test'
            assert records[0]['conversation_id'] == 'synthetic-conversation'
            assert records[0]['urls'] == ['/images/synthetic.png']
        finally:
            release.set()
            await holder
            pool.shutdown()
    asyncio.run(check())


@pytest.mark.parametrize('cancelled', [False, True])
def test_stream_terminal_log_once_for_success_or_disconnect(monkeypatch, cancelled):
    async def check():
        rows = []
        closed = threading.Event()
        monitor = RealtimeMonitorService()
        monkeypatch.setattr(logs, 'realtime_monitor_service', monitor)
        monkeypatch.setattr(logs.log_service, 'add', lambda *a, **kw: rows.append(a[-1]))
        def output():
            try:
                yield {'type': 'image_generation.completed', 'data': []}
                yield {'type': 'image_generation.completed', 'data': []}
            finally:
                closed.set()
        call = logs.LoggedCall({}, '/v1/images/generations', 'gpt-image-2', 'synthetic')
        response = await call.run(output)
        if cancelled:
            await anext(response.body_iterator)
            await response.body_iterator.aclose()
        else:
            chunks = [chunk async for chunk in response.body_iterator]
            assert len(chunks) == 4
        assert closed.is_set()
        assert len(rows) == 1
        assert rows[0]['status'] == ('failed' if cancelled else 'success')
        if cancelled:
            assert rows[0]['error'] == 'stream_closed_before_completion'
        assert monitor.operations_snapshot()['active_requests'] == 0
    asyncio.run(check())


def test_execution_rejection_log_keeps_http_error_fields(monkeypatch):
    async def check():
        pool = BoundedExecution('review-full', 1, 0, 0.05)
        entered = threading.Event()
        release = threading.Event()
        rows = []
        monkeypatch.setattr(logs, '_IMAGE_POOL', pool)
        monkeypatch.setattr(logs.log_service, 'add', lambda *args, **kw: rows.append(args[-1]))
        def hold():
            entered.set()
            release.wait(3)
        holder = asyncio.create_task(pool.run(hold))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            call = logs.LoggedCall({}, '/v1/images/generations', 'gpt-image-2', 'synthetic')
            response = await call.run(lambda: {'data': []})
            print('EXECUTION_BUSY ' + json.dumps({'http_status': response.status_code, 'body': response.body.decode(), 'log': rows[-1]}, ensure_ascii=False))
            assert response.status_code == 503
            assert rows[-1].get('status_code') == 503
            assert rows[-1].get('failure_code') == 'image_execution_busy'
        finally:
            release.set()
            await holder
            pool.shutdown()
    asyncio.run(check())


def test_cancelled_scope_still_finishes_stream_monitor(monkeypatch):
    import anyio
    async def check():
        rows = []
        closed = threading.Event()
        monitor = RealtimeMonitorService()
        monkeypatch.setattr(logs, 'realtime_monitor_service', monitor)
        monkeypatch.setattr(logs.log_service, 'add', lambda *a, **kw: rows.append(a[-1]))
        def output():
            try:
                yield {'type': 'image_generation.in_progress'}
                yield {'type': 'image_generation.completed', 'data': []}
            finally:
                closed.set()
        call = logs.LoggedCall({}, '/v1/images/generations', 'gpt-image-2', 'synthetic')
        response = await call.run(output)
        await anext(response.body_iterator)
        with anyio.CancelScope() as scope:
            scope.cancel()
            await response.body_iterator.aclose()
        assert monitor.operations_snapshot()['active_requests'] == 0
        assert closed.is_set()
        assert len(rows) == 1
        assert rows[0]['status'] == 'failed'
    asyncio.run(check())


def test_stream_upstream_failure_keeps_original_code(monkeypatch):
    from services.image_failure import ImageGenerationError
    async def check():
        rows = []
        monkeypatch.setattr(logs.log_service, 'add', lambda *a, **kw: rows.append(a[-1]))
        def output():
            yield {'type': 'image_generation.in_progress'}
            raise ImageGenerationError('synthetic failure', code='file_upload_throttled')
        call = logs.LoggedCall({}, '/v1/images/generations', 'gpt-image-2', 'synthetic')
        response = await call.run(output)
        payload = ''.join([chunk async for chunk in response.body_iterator])
        assert 'event: error' in payload
        assert 'file_upload_throttled' in payload
        assert len(rows) == 1
        assert rows[0]['status_code'] == 429
        assert rows[0]['failure_code'] == 'file_upload_throttled'
    asyncio.run(check())


def test_log_preparation_failure_does_not_change_success(monkeypatch):
    async def check():
        monitor = RealtimeMonitorService()
        monkeypatch.setattr(logs, 'realtime_monitor_service', monitor)
        call = logs.LoggedCall({}, '/v1/images/generations', 'gpt-image-2', 'synthetic')
        def broken(*args, **kwargs):
            raise RuntimeError('synthetic diagnostic error')
        monkeypatch.setattr(call, '_prepare_log', broken)
        result = await call.run(lambda: {'data': []})
        assert result == {'data': []}
        assert monitor.operations_snapshot()['active_requests'] == 0
    asyncio.run(check())


def test_asgi_disconnect_closes_inflight_stream_and_logs_once(monkeypatch):
    async def check():
        pool = BoundedExecution('asgi-disconnect', 1, 1, 0.5)
        release, entered, closed = threading.Event(), threading.Event(), threading.Event()
        disconnect = asyncio.Event()
        rows, sent = [], []
        monitor = RealtimeMonitorService()
        monkeypatch.setattr(logs, '_IMAGE_POOL', pool)
        monkeypatch.setattr(logs, 'realtime_monitor_service', monitor)
        monkeypatch.setattr(logs.log_service, 'add', lambda *a, **kw: rows.append(a[-1]))
        def output():
            try:
                yield {'type': 'image_generation.in_progress'}
                entered.set()
                release.wait(3)
                yield {'type': 'image_generation.completed', 'data': []}
            finally:
                closed.set()
        async def receive():
            await disconnect.wait()
            return {'type': 'http.disconnect'}
        async def send(message):
            sent.append(message)
        call = logs.LoggedCall({}, '/v1/images/generations', 'gpt-image-2', 'synthetic')
        response = await call.run(output)
        task = asyncio.create_task(response({'type': 'http', 'asgi': {'spec_version': '2.0'}}, receive, send))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            disconnect.set()
            await asyncio.wait_for(task, timeout=1)
            assert monitor.operations_snapshot()['active_requests'] == 0
            assert pool.snapshot()['active'] == 1
            assert len(rows) == 1
            assert rows[0]['error'] == 'stream_closed_before_completion'
        finally:
            release.set()
            await task
            for _ in range(100):
                if closed.is_set() and pool.snapshot()['active'] == 0:
                    break
                await asyncio.sleep(0.005)
            pool.shutdown()
        assert closed.is_set()
        assert len(rows) == 1
    asyncio.run(check())


def test_stream_completed_work_does_not_stay_active_during_log_write(monkeypatch):
    async def check():
        entered, release = threading.Event(), threading.Event()
        monitor = RealtimeMonitorService()
        monkeypatch.setattr(logs, 'realtime_monitor_service', monitor)
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(3)
        monkeypatch.setattr(logs.log_service, 'add', blocked)
        call = logs.LoggedCall({}, '/v1/images/generations', 'gpt-image-2', 'synthetic')
        response = await call.run(lambda: iter([{'type': 'image_generation.completed', 'data': []}]))
        async def consume():
            return [chunk async for chunk in response.body_iterator]
        task = asyncio.create_task(consume())
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            assert not task.done()
            assert monitor.operations_snapshot()['active_requests'] == 0
            assert logs.image_threadpool_snapshot()['active'] == 0
        finally:
            release.set()
            await task
    asyncio.run(check())
