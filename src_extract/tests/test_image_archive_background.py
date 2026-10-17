"""No upstream calls: exercise real queue admission and shared log receipts."""
import time
import threading
from types import SimpleNamespace

import pytest
from services.image_archive_service import ImageArchiveService, enrich_archive_detail
from services.call_record_service import CallRecordService
from services.storage.image_archive_repository import load_receipts
from services.protocol import conversation as protocol
from services import log_service as logs
from test_image_result_count import multi_asset


@pytest.fixture
def archive(tmp_path):
    service = ImageArchiveService(database_url=f"sqlite:///{(tmp_path / 'archive.db').as_posix()}")
    yield service
    service.shutdown()


@pytest.mark.parametrize('n', [1, 2])
def test_primary_response_finishes_while_extras_are_blocked(monkeypatch, multi_asset, archive, n):
    release = threading.Event()
    extra_started = threading.Event()
    saved = []
    monkeypatch.setattr(protocol, 'image_archive_service', archive)
    monkeypatch.setattr(protocol, 'config', SimpleNamespace(image_request_timeout_secs=0.5,
                                                          image_parallel_generation=True))
    def save(data, *args, **kwargs):
        if data != multi_asset.images[0]:
            extra_started.set()
            assert release.wait(5)
        saved.append(data)
        return f'http://localhost/images/{len(saved)}.png'
    monkeypatch.setattr(protocol, 'save_image_bytes', save)
    def generate(request, index, total):
        return [protocol._image_result_output_from_urls(
            multi_asset.backend, request, 'fixture', multi_asset.urls, index, total)]
    monkeypatch.setattr(protocol, '_generate_single_image', generate)
    try:
        start = time.monotonic()
        outputs = list(protocol.stream_image_outputs_with_pool(
            protocol.ConversationRequest(model='gpt-image-2', n=n)))
        assert sum(len(item.data) for item in outputs) == n
        assert time.monotonic() - start < 0.5
        assert saved == [multi_asset.images[0]] * n
        assert sum(item.result_counts['image_extra_pending_count'] for item in outputs) == 2 * n
        assert extra_started.wait(1)
        print({'n': n, 'primary_count': n, 'extra_pending': 2*n, 'response_ms': round((time.monotonic()-start)*1000)})
    finally:
        release.set()


@pytest.mark.parametrize('fail', [False, True])
def test_receipts_enrich_logs_across_instances_without_billing_extras(monkeypatch, multi_asset, archive, fail):
    release = threading.Event()
    monkeypatch.setattr(protocol, 'image_archive_service', archive)
    def save(data, *args, **kwargs):
        if data != multi_asset.images[0]:
            assert release.wait(5)
            if fail:
                raise OSError('synthetic archive failure')
        return 'http://localhost/images/' + str(multi_asset.images.index(data)) + '.png'
    monkeypatch.setattr(protocol, 'save_image_bytes', save)
    output = protocol._image_result_output_from_urls(multi_asset.backend,
        protocol.ConversationRequest(model='gpt-image-2', n=1), 'fixture', multi_asset.urls, 1, 1)
    result = protocol.collect_image_outputs([output])
    service = CallRecordService(database_url=archive.database_url)
    item = {'id': 'fixture-log', 'type': 'call', 'detail': {
        'endpoint': '/v1/images/generations', 'status': 'success', 'request_meta': {'n': 1},
        'urls': result['_image_urls'], **logs.image_result_metrics(result)}}
    service.append_item(item)
    try:
        assert service.get_detail('fixture-log')['raw_detail']['image_extra_pending_count'] == 2
        assert '_image_extras' not in logs._strip_internal_response_fields(result)
    finally:
        release.set()
    archive.runner.shutdown(wait=True)
    # New service instance uses the same DB, not in-memory worker state.
    reader = CallRecordService(database_url=archive.database_url)
    detail = reader.get_detail('fixture-log')
    raw = detail['raw_detail']
    assert raw['image_extra_pending_count'] == 0
    assert raw['image_extra_failed_count'] == (2 if fail else 0)
    assert raw['image_extra_saved_count'] == (0 if fail else 2)
    assert len(detail['image_urls']) == (1 if fail else 3)
    assert raw['image_returned_count'] == 1
    from services.call_view import call_image_counts
    assert call_image_counts({'detail': raw})[:2] == (1, 1)
    assert enrich_archive_detail(raw, reader.repository.engine) == raw


def test_queue_full_and_byte_budget_never_wait_or_overallocate(tmp_path):
    service = ImageArchiveService(database_url=f"sqlite:///{(tmp_path / 'bounded.db').as_posix()}",
                                  max_workers=1, queue_size=1, max_bytes=10)
    release = threading.Event()
    entered = threading.Event()
    def save(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return 'http://localhost/images/extra.png'
    try:
        first = service.submit(b'abc', '', slot=1, call_id='', save=save)
        assert entered.wait(1)
        second = service.submit(b'def', '', slot=1, call_id='', save=save)
        assert service.submit(b'ghi', '', slot=1, call_id='', save=save)['reason'] == 'archive_queue_full'
        assert service.submit(b'123456', '', slot=1, call_id='', save=save)['reason'] == 'archive_memory_limit'
        assert first['status'] == second['status'] == 'pending'
        assert service._bytes == 6
    finally:
        release.set()
        service.shutdown()
    assert service._bytes == 0
    assert service.submit(b'x', '', slot=1, call_id='', save=save)['status'] == 'failed'
    receipts = load_receipts(service._engine, [first['archive_id'], second['archive_id']])
    assert len(receipts) == 2


def test_missing_receipt_after_restart_is_interrupted_not_saved(archive):
    reader = CallRecordService(database_url=archive.database_url)
    detail = {'extra_images': [{'archive_id': 'lost', 'status': 'pending', 'expires_at': 1}],
              'image_extra_pending_count': 1, 'image_saved_count': 1}
    result = enrich_archive_detail(detail, reader.repository.engine)
    assert result['extra_images'][0]['reason'] == 'archive_interrupted'
    assert result['image_extra_pending_count'] == 0
    assert result['image_extra_failed_count'] == 1
    assert result['image_saved_count'] == 1


def test_pending_archives_keep_upstream_conversation(monkeypatch):
    monkeypatch.setattr(protocol, 'config', SimpleNamespace(image_remove_conversation_after_result=True))
    deleted = []
    output = protocol.ImageOutput(kind='result', model='gpt-image-2', index=1, total=1,
        conversation_id='fixture', result_counts={'image_extra_pending_count': 1})
    protocol._cleanup_image_conversations_after_success(SimpleNamespace(delete_conversation=deleted.append), [output])
    assert deleted == []


def test_expired_queue_item_never_starts_storage(tmp_path):
    service = ImageArchiveService(database_url=f"sqlite:///{(tmp_path / 'expired.db').as_posix()}",
                                  max_age_seconds=0)
    saves = []
    entry = service.submit(b'fixture', '', slot=1, call_id='fixture',
                           save=lambda *a, **kw: saves.append(True))
    service.runner.shutdown(wait=True)
    result = load_receipts(service._engine, [entry['archive_id']])[entry['archive_id']]
    assert result['status'] == 'failed' and result['reason'] == 'archive_queue_expired'
    assert saves == [] and service._bytes == 0


def test_receipt_write_failure_releases_capacity_without_repeating_storage(monkeypatch, archive):
    from services import image_archive_service as module
    def fail(*a, **kw):
        raise OSError('synthetic receipt failure')
    monkeypatch.setattr(module, 'save_receipt', fail)
    saved = []
    def save(*a, **kw):
        saved.append(True)
        return 'http://localhost/images/extra.png'
    entry = archive.submit(b'fixture', '', slot=1, call_id='fixture', save=save)
    assert entry['status'] == 'pending'
    archive.runner.shutdown(wait=True)
    assert saved == [True]
    assert archive._bytes == archive.runner.status()['accepted'] == 0
    assert load_receipts(archive._engine, [entry['archive_id']]) == {}


def test_receipt_read_failure_preserves_primary_log(monkeypatch, archive):
    from services import image_archive_service as module
    def fail(*a, **kw):
        raise OSError('synthetic receipt read failure')
    monkeypatch.setattr(module, 'load_receipts', fail)
    detail = {'status': 'success', 'image_returned_count': 1, 'image_saved_count': 1,
              'extra_images': [{'archive_id': 'pending', 'status': 'pending'}]}
    result = enrich_archive_detail(detail, None)
    assert result['status'] == 'success'
    assert result['image_returned_count'] == result['image_saved_count'] == 1
    assert result['image_archive_status_error']


def test_archive_worker_can_restart_after_clean_shutdown(archive):
    archive.shutdown()
    archive.start()
    entry = archive.submit(b'fixture', '', slot=1, call_id='fixture',
                           save=lambda *a, **kw: 'http://localhost/images/extra.png')
    archive.runner.shutdown(wait=True)
    assert load_receipts(archive._engine, [entry['archive_id']])[entry['archive_id']]['status'] == 'saved'


def test_receipt_cleanup_keeps_recent_entries(archive):
    from services.storage.image_archive_repository import ImageArchiveResult, cleanup_receipts
    service = CallRecordService(database_url=archive.database_url)
    with service.repository.engine.begin() as connection:
        connection.execute(ImageArchiveResult.__table__.insert(), [
            {'id': 'old', 'completed_at': 1, 'payload': {'status': 'saved'}},
            {'id': 'recent', 'completed_at': time.time(), 'payload': {'status': 'saved'}},
        ])
    cleanup_receipts(service.repository.engine, time.time() - 60)
    assert set(load_receipts(service.repository.engine, ['old', 'recent'])) == {'recent'}
