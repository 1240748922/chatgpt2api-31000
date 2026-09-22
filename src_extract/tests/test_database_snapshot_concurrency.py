"""Real SQLite/WAL transactions; no accounts, credentials or upstream I/O.

Commit another replica's writes while a snapshot SELECT is still being read.
This deterministically reproduces the old three-read revision race without
depending on thread scheduling or a large production account collection.
"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.exc import NoResultFound

from services.application_database import dispose_database_engine
from services.storage.base import StorageMutation, StorageRevisionConflictError
from services.storage.database_storage import DatabaseStorageBackend


@pytest.fixture
def databases(tmp_path):
    url = f"sqlite:///{(tmp_path / 'snapshots.db').as_posix()}"
    reader = DatabaseStorageBackend(url)
    writer = DatabaseStorageBackend(url)
    try:
        yield reader, writer
    finally:
        dispose_database_engine(url)


@pytest.fixture(params=[("accounts", "access_token"), ("auth_keys", "id")])
def collection(databases, request):
    reader, writer = databases
    name, key = request.param
    return SimpleNamespace(
        reader=reader, writer=writer, name=name, key=key,
        load=getattr(reader, f"load_{name}_snapshot"),
        latest=getattr(writer, f"load_{name}_snapshot"),
        replace=getattr(reader, f"replace_{name}"),
        mutate=getattr(writer, f"mutate_{name}"),
    )


@contextmanager
def write_during_snapshot(backend, collection, write):
    """Interleave a committed write after SELECT execution, before row fetch."""
    active = False

    def after_select(conn, cursor, statement, parameters, context, executemany):
        nonlocal active
        if active or not statement.lstrip().upper().startswith("SELECT"):
            return
        if f"ORDER BY {collection}.id" not in statement:
            return
        active = True
        try:
            write()
        finally:
            active = False

    event.listen(backend.engine, "after_cursor_execute", after_select)
    try:
        yield
    finally:
        event.remove(backend.engine, "after_cursor_execute", after_select)


@pytest.mark.parametrize("populated", [False, True])
def test_snapshot_is_one_select_including_empty_collections(collection, populated):
    c = collection
    items = [{c.key: "second"}, {c.key: "first"}] if populated else []
    revision = c.replace(items).revision
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(c.reader.engine, "before_cursor_execute", record)
    try:
        snapshot = c.load()
    finally:
        event.remove(c.reader.engine, "before_cursor_execute", record)
    assert snapshot.items == items
    assert snapshot.revision == revision
    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("SELECT")
    assert "FOR UPDATE" not in statements[0].upper()


@pytest.mark.parametrize("keys", [(), ("missing",), ("a", "a", "missing")])
def test_partial_conflict_snapshot_is_bounded_and_keeps_coherent_revision(databases, keys):
    reader, writer = databases
    revision = reader.replace_accounts([{"access_token": "a", "quota": 8},
                                        {"access_token": "unrelated", "quota": 4}]).revision
    statements, decoded = [], []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    original = reader._deserialize
    def decode(data):
        decoded.append(data)
        return original(data)
    reader._deserialize = decode
    event.listen(reader.engine, "before_cursor_execute", record)
    try:
        snapshot = reader.load_accounts_subset_snapshot(keys)
    finally:
        event.remove(reader.engine, "before_cursor_execute", record)
    expected = [{"access_token": "a", "quota": 8}] if "a" in keys else []
    assert snapshot.items == expected and snapshot.revision == revision
    assert len(statements) == 1 and len(decoded) == len(expected)
    with write_during_snapshot(reader, "accounts", lambda: writer.upsert_account({"access_token": "a", "quota": 2})):
        snapshot = reader.load_accounts_subset_snapshot(keys)
    assert snapshot.items == expected and snapshot.revision == revision


def test_continuous_writes_cannot_starve_or_mislabel_snapshot(collection):
    c = collection
    expected = [{c.key: "existing", "generation": 0}]
    revision = c.replace(expected).revision
    writes = 0

    def write():
        nonlocal writes, expected, revision
        writes += 1
        changed = {c.key: "existing", "generation": writes}
        imported = {c.key: f"imported-{writes}", "generation": writes}
        revision = c.mutate(StorageMutation(upserts=(changed, imported))).revision
        expected = [changed, *expected[1:], imported]

    with write_during_snapshot(c.reader, c.name, write):
        for _ in range(8):
            before_items, before_revision = expected, revision
            snapshot = c.load()
            # A coherent older snapshot is valid; old rows with a newer revision
            # would let CAS replacement silently erase a concurrent import.
            assert snapshot.items == before_items
            assert snapshot.revision == before_revision

    assert writes == 8  # Writers made progress during every read, without locks.
    with pytest.raises(StorageRevisionConflictError):
        c.replace(snapshot.items, expected_revision=snapshot.revision)
    assert c.latest().items == expected
    assert c.latest().revision == revision


def test_empty_snapshot_keeps_its_revision_during_concurrent_insert(collection):
    c = collection
    revision = c.latest().revision
    inserted = {c.key: "imported"}
    with write_during_snapshot(
        c.reader, c.name, lambda: c.mutate(StorageMutation(upserts=(inserted,))),
    ):
        snapshot = c.load()
    assert snapshot.items == []
    assert snapshot.revision == revision
    assert c.latest().items == [inserted]
    with pytest.raises(StorageRevisionConflictError):
        c.replace([], expected_revision=revision)


def test_snapshot_deserialization_does_not_need_writers_to_stop(collection, monkeypatch):
    c = collection
    original = {c.key: "existing", "generation": 0}
    revision = c.replace([original]).revision
    deserialize = c.reader._deserialize
    writes = 0

    def decode(value):
        nonlocal writes
        writes += 1
        c.mutate(StorageMutation(upserts=({**original, "generation": writes},)))
        return deserialize(value)

    monkeypatch.setattr(c.reader, "_deserialize", decode)
    snapshot = c.load()
    assert snapshot.items == [original]
    assert snapshot.revision == revision
    assert writes == 1
    assert c.latest().items[0]["generation"] == 1


def test_invalid_payloads_are_skipped_without_losing_revision(collection):
    c = collection
    valid = {c.key: "valid"}
    revision = c.replace([valid, {c.key: "bad-json"}, {c.key: "not-object"}]).revision
    model, model_key = c.reader._spec(c.name)
    with c.writer.Session.begin() as session:
        for key, payload in (("bad-json", "{"), ("not-object", "[]")):
            row = session.query(model).filter(getattr(model, model_key) == key).one()
            row.data = payload
    snapshot = c.load()
    assert snapshot.items == [valid]
    assert snapshot.revision == revision


def test_missing_revision_is_not_reported_as_an_empty_pool(collection):
    from services.storage.database_storage import StorageRevisionModel

    c = collection
    with c.writer.Session.begin() as session:
        session.delete(session.get(StorageRevisionModel, c.name))
    with pytest.raises(NoResultFound, match=f"missing {c.name} storage revision"):
        c.load()


def test_parallel_readers_observe_only_complete_commits(collection):
    c = collection

    def generation(number):
        # Vary both membership and values in one atomic write.
        return [{c.key: f"item-{i:03}", "generation": number}
                for i in range(63 + number % 2)]

    c.replace(generation(1))
    start = Barrier(5)

    def read_many():
        start.wait(timeout=10)
        for _ in range(32):
            snapshot = c.load()
            version = int(snapshot.revision.split(":")[1])
            assert snapshot.items == generation(version)

    def write_many():
        start.wait(timeout=10)
        for number in range(2, 34):
            result = getattr(c.writer, f"replace_{c.name}")(generation(number))
            assert result.revision == f"{c.name}:{number}"

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(read_many) for _ in range(4)]
        futures.append(pool.submit(write_many))
        for future in futures:
            future.result(timeout=30)
    assert c.latest().items == generation(33)


def test_image_request_survives_snapshot_churn_during_token_rotation(databases, monkeypatch):
    """Selection -> RT renewal -> CAS conflict -> reload -> generation result.

Only the upstream token exchange/image response and result bookkeeping are
stubbed; selection, slot leasing, token rotation and database CAS are real.
"""
    import services.account_service as accounts_module
    from services.account_service import AccountService
    from services.protocol import conversation as protocol

    reader, writer = databases
    # Keep exercising the legacy full-collection CAS/reload fallback here;
    # targeted credential CAS has separate continuous-write regressions.
    monkeypatch.setattr(reader, "mutate_accounts_checked", None, raising=False)
    reader.replace_accounts([{
        "access_token": "old-test-token", "refresh_token": "test-refresh",
        "status": "正常", "quota": 8, "image_quota_unknown": False,
    }])
    monkeypatch.setattr(AccountService, "_load_cumulative_total", lambda self: len(self._accounts))
    monkeypatch.setattr(accounts_module.log_service, "add", lambda *a, **kw: None)
    service = AccountService(reader)
    service._image_shard_count = 1
    monkeypatch.setattr(service, "_token_needs_refresh", lambda token, **kw: token == "old-test-token")
    monkeypatch.setattr(service, "_request_access_token_refresh", lambda *a, **kw: {
        "access_token": "new-test-token", "refresh_token": "new-test-refresh",
    })
    # Stale local revision guarantees a real CAS conflict when saving the AT.
    imported = {"access_token": "imported-test-token", "status": "正常", "quota": 7}
    writer.upsert_account(imported)
    writes = 0

    def write():
        nonlocal writes
        if writes < 3:
            writes += 1
            writer.upsert_account({**imported, "success": writes})

    monkeypatch.setattr(protocol, "account_service", service)
    monkeypatch.setattr(protocol, "config", SimpleNamespace(
        image_account_retry_enabled=True, image_max_account_attempts=4,
        image_stream_timeout_secs=90,
    ))
    monkeypatch.setattr(protocol, "proxy_settings", SimpleNamespace(
        acquire_image_egress=lambda *a, **kw: 0,
        get_fallback_proxy_reference=lambda: "",
    ))
    monkeypatch.setattr(protocol, "_cleanup_image_conversations_after_success", lambda *a: None)
    selected, closed = [], []

    class Backend:
        def __init__(self, access_token, **kw):
            self.token = access_token
            self.proxy_profile = SimpleNamespace()
            selected.append(access_token)

        def close(self):
            closed.append(self.token)

    def output(backend, request, index, total):
        yield protocol.ImageOutput(kind="result", model=request.model, index=index,
                                   total=total, data=[{"url": "/images/test.png"}])

    monkeypatch.setattr(protocol, "OpenAIBackendAPI", Backend)
    monkeypatch.setattr(protocol, "stream_image_outputs", output)
    monkeypatch.setattr(service, "mark_image_result", lambda token, *a, **kw: service.release_image_slot(token))
    try:
        with write_during_snapshot(reader, "accounts", write):
            outputs = protocol._generate_single_image(protocol.ConversationRequest(
                model="gpt-image-2", images=["test-reference"],
            ), 1, 1)
        assert writes == 3
        assert outputs[0].kind == "result"
        assert len(outputs[0].image_attempts) == 1
        assert selected == closed == ["new-test-token"]
        assert service._image_inflight == {}
        rows = {item["access_token"]: item for item in writer.load_accounts()}
        assert "old-test-token" not in rows
        assert rows["new-test-token"]["refresh_token"] == "new-test-refresh"
        assert rows["imported-test-token"]["success"] == 3
    finally:
        service._image_result_persist_executor.shutdown(wait=True)
