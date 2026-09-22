"""Account selection must survive writes to unrelated accounts on another replica."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

import services.account_service as accounts_module
from services.account_service import AccountService, ImageAccountSelectionError, RefreshCredentialsChangedError
from services.protocol import conversation as protocol
from services.storage.base import StorageMutation, StorageRevisionConflictError
from test_database_snapshot_concurrency import databases


@pytest.fixture
def account_flow(databases, monkeypatch):
    reader, writer = databases
    reader.replace_accounts([{
        "access_token": "old-test-token", "refresh_token": "test-refresh",
        "status": "正常", "quota": 8, "image_quota_unknown": False,
    }])
    monkeypatch.setattr(AccountService, "_load_cumulative_total", lambda self: len(self._accounts))
    monkeypatch.setattr(accounts_module.log_service, "add", lambda *a, **kw: None)
    service = AccountService(reader)
    service._image_shard_count = 1
    service._ACCOUNT_SNAPSHOT_TTL_SECONDS = 3600
    monkeypatch.setattr(service, "_token_needs_refresh", lambda token, **kw: token == "old-test-token")
    exchanges = []

    def exchange(*a, **kw):
        exchanges.append(True)
        return {"access_token": "new-test-token", "refresh_token": "new-test-refresh"}

    monkeypatch.setattr(service, "_request_access_token_refresh", exchange)
    monkeypatch.setattr(protocol, "account_service", service)
    monkeypatch.setattr(protocol, "config", SimpleNamespace(
        image_account_retry_enabled=True, image_max_account_attempts=4,
        image_stream_timeout_secs=90,
    ))
    monkeypatch.setattr(protocol, "proxy_settings", SimpleNamespace(
        acquire_image_egress=lambda *a, **kw: 0, get_fallback_proxy_reference=lambda: "",
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

    def run():
        return protocol._generate_single_image(protocol.ConversationRequest(model="gpt-image-2"), 1, 1)

    try:
        yield SimpleNamespace(service=service, reader=reader, writer=writer, run=run,
                              selected=selected, closed=closed, exchanges=exchanges)
    finally:
        service._image_result_persist_executor.shutdown(wait=True)


def test_continuous_unrelated_writes_do_not_exhaust_token_save_retries(account_flow, monkeypatch):
    f = account_flow
    writes = []

    def racing(original):
        def save(mutation, **kwargs):
            writes.append(True)
            f.writer.upsert_account({"access_token": "other-replica-import", "success": len(writes)})
            return original(mutation, **kwargs)
        return save

    for name in ("mutate_accounts", "mutate_accounts_checked"):
        original = getattr(f.reader, name, None)
        if callable(original):
            monkeypatch.setattr(f.reader, name, racing(original))
    before_revision = f.service._accounts_revision
    reads = []
    original_load = f.reader.load_accounts_snapshot

    def load():
        reads.append(True)
        return original_load()

    monkeypatch.setattr(f.reader, "load_accounts_snapshot", load)
    outputs = f.run()
    assert outputs[0].kind == "result"
    assert len(outputs[0].image_attempts) == 1
    assert f.selected == f.closed == ["new-test-token"]
    assert f.exchanges == [True]
    assert len(writes) == 1
    assert reads == []
    assert f.service._image_inflight == {}
    rows = {item["access_token"]: item for item in f.writer.load_accounts()}
    assert "old-test-token" not in rows
    assert rows["other-replica-import"]["success"] == 1
    assert f.service._accounts_revision == before_revision
    # A partial credential write must not hide the import behind a newer
    # collection revision, or falsely acknowledge unrelated local quota writes.
    f.service._account_snapshot_checked_at = 0
    assert f.service._refresh_accounts_snapshot_if_stale()
    assert "other-replica-import" in f.service._accounts


def test_selector_releases_conflicted_candidate_and_uses_next_account(account_flow, monkeypatch):
    f = account_flow
    ready = {**f.service._accounts["old-test-token"], "access_token": "healthy-test-token", "refresh_token": ""}
    f.service._accounts["healthy-test-token"] = ready

    def ensure(token, **kwargs):
        if token == "old-test-token":
            raise StorageRevisionConflictError("accounts", "accounts:1", "accounts:2")
        return token

    monkeypatch.setattr(f.service, "ensure_access_token", ensure)
    outputs = f.run()
    assert outputs[0].kind == "result"
    assert f.selected == f.closed == ["healthy-test-token"]
    assert f.service._image_inflight == {}


def test_token_save_does_not_write_or_acknowledge_other_pending_accounts(account_flow):
    f = account_flow
    other = deepcopy(f.service._accounts["old-test-token"])
    other.update(access_token="pending-other", refresh_token="", success=0, quota=8)
    f.writer.upsert_account(other)
    f.service._accounts["pending-other"] = {**other, "success": 1, "quota": 7}
    f.service._persisted_accounts["pending-other"] = deepcopy(other)
    assert f.run()[0].kind == "result"
    remote = {item["access_token"]: item for item in f.writer.load_accounts()}
    assert remote["pending-other"]["success"] == 0
    assert f.service._persisted_accounts["pending-other"]["success"] == 0
    assert f.service._accounts["pending-other"]["success"] == 1


@pytest.mark.parametrize("change", ["quota", "legacy_defaults"])
def test_targeted_save_merges_real_same_account_change(account_flow, change):
    f = account_flow
    if change == "quota":
        remote = {**f.writer.load_accounts()[0], "quota": 4, "file_upload_blocked_until": 5000000000}
    else:
        # The service's normalized snapshot contains defaults absent in the DB.
        # A retry must use the raw snapshot as its CAS baseline, not retry the
        # same normalization mismatch until the budget is exhausted.
        remote = {"access_token": "old-test-token", "refresh_token": "test-refresh",
                  "status": "正常", "quota": 4, "image_quota_unknown": False}
    f.writer.upsert_account(remote)
    assert f.run()[0].kind == "result"
    rows = f.writer.load_accounts()
    assert len(rows) == 1
    assert rows[0]["access_token"] == "new-test-token"
    assert rows[0]["quota"] == 4
    if change == "quota":
        assert rows[0]["file_upload_blocked_until"] == 5000000000


@pytest.mark.parametrize("unrelated_count", [1, 11000])
def test_credential_quota_conflict_does_not_reload_the_whole_pool(account_flow, monkeypatch, unrelated_count):
    f = account_flow
    old = deepcopy(f.writer.load_accounts()[0])
    f.writer.upsert_account({**old, "quota": 3, "file_upload_blocked_until": 5000000000})
    f.writer.mutate_accounts(StorageMutation(upserts=tuple(
        {"access_token": f"unrelated-import-{index}", "quota": 5} for index in range(unrelated_count))))
    monkeypatch.setattr(f.reader, "load_accounts_snapshot", lambda:
                        pytest.fail("credential conflict reloaded the whole account pool"))
    assert f.run()[0].kind == "result"
    rows = {row["access_token"]: row for row in f.writer.load_accounts()}
    assert rows["new-test-token"]["quota"] == 3
    assert rows["new-test-token"]["file_upload_blocked_until"] == 5000000000
    assert rows["unrelated-import-0"]["quota"] == 5
    assert len(rows) == unrelated_count + 1
    assert "old-test-token" not in rows


def test_conflicted_remote_rotation_still_follows_authoritative_identity(account_flow):
    f = account_flow
    old = deepcopy(f.service._accounts["old-test-token"])
    remote = {**old, "access_token": "remote-winner", "refresh_token": "remote-refresh",
              "last_token_refresh_at": "remote-generation"}
    f.writer.mutate_accounts(StorageMutation(upserts=(remote,), delete_keys=("old-test-token",)))
    f.service._image_inflight["old-test-token"] = 1
    with pytest.raises(RefreshCredentialsChangedError):
        f.service._apply_refreshed_tokens(
            "old-test-token", {"access_token": "stale-winner", "refresh_token": "stale-refresh"},
            "test", expected_refresh_token="test-refresh")
    assert f.writer.load_accounts() == [remote]
    assert f.service.resolve_access_token("old-test-token") == "remote-winner"
    assert f.service._image_inflight == {"remote-winner": 1}
    f.service.release_image_slot("old-test-token")
    assert f.service._image_inflight == {}


@pytest.mark.parametrize("change", ["deleted", "credentials", "destination_collision"])
def test_stale_token_rotation_does_not_overwrite_remote_credentials(account_flow, change):
    f = account_flow
    old = deepcopy(f.service._accounts["old-test-token"])
    if change == "deleted":
        f.writer.delete_account("old-test-token")
    elif change == "credentials":
        f.writer.upsert_account({**old, "refresh_token": "replacement-refresh",
                                 "last_token_refresh_at": "replacement-generation"})
    else:
        f.writer.upsert_account({**old, "access_token": "new-test-token",
                                 "management_id": "acct_different", "refresh_token": "unrelated-refresh"})
    before = f.writer.load_accounts()
    with pytest.raises(RefreshCredentialsChangedError):
        f.service._apply_refreshed_tokens(
            "old-test-token", {"access_token": "new-test-token", "refresh_token": "new-test-refresh"},
            "test", expected_refresh_token="test-refresh",
            expected_last_token_refresh_at=old.get("last_token_refresh_at"),
        )
    assert f.writer.load_accounts() == before
    assert f.service._token_aliases.get("old-test-token") != "new-test-token"


def test_refresh_error_record_ignores_unrelated_revision_churn(account_flow, monkeypatch):
    f = account_flow
    before_revision = f.service._accounts_revision
    writes = []
    original = f.reader.mutate_accounts_checked

    def save(mutation, **kwargs):
        writes.append(True)
        f.writer.upsert_account({"access_token": "unrelated", "quota": 1})
        return original(mutation, **kwargs)

    monkeypatch.setattr(f.reader, "mutate_accounts_checked", save)
    assert f.service._record_token_refresh_error(
        "old-test-token", "test", "temporary refresh error",
        expected_access_token="old-test-token", expected_refresh_token="test-refresh",
    )
    assert writes == [True]
    assert f.service._accounts_revision == before_revision
    rows = {item["access_token"]: item for item in f.writer.load_accounts()}
    assert rows["old-test-token"]["last_token_refresh_error"] == "temporary refresh error"
    assert "unrelated" in rows


def test_conflict_fallback_stops_at_request_deadline_and_keeps_account_status(account_flow, monkeypatch):
    from test_image_recovery import Clock

    f = account_flow
    clock = Clock()
    monkeypatch.setattr(accounts_module, "time", clock)

    def ensure(*a, **kw):
        clock.sleep(2)
        raise StorageRevisionConflictError("accounts", "old", "new")

    monkeypatch.setattr(f.service, "ensure_access_token", ensure)
    with pytest.raises(ImageAccountSelectionError) as caught:
        f.service.get_available_access_token(deadline_monotonic=clock.now + 1)
    assert caught.value.code == "task_interrupted"
    assert f.service._image_inflight == {}
    assert f.service._accounts["old-test-token"]["status"] == "正常"


def test_conflict_fallback_does_not_recycle_candidates_forever(account_flow, monkeypatch):
    f = account_flow
    f.service._IMAGE_POOL_WAIT_SECONDS = 0
    calls = []

    def ensure(token, **kw):
        calls.append(token)
        raise StorageRevisionConflictError("accounts", "old", "new")

    monkeypatch.setattr(f.service, "ensure_access_token", ensure)
    with pytest.raises(ImageAccountSelectionError):
        f.service.get_available_access_token()
    assert calls == ["old-test-token"]
    assert f.service._image_inflight == {}


def test_checked_mutation_ignores_only_unrelated_changes(databases):
    reader, writer = databases
    a, b = {"access_token": "a", "quota": 8}, {"access_token": "b", "quota": 8}
    baseline = reader.replace_accounts([a, b]).revision
    writer.upsert_account({**b, "quota": 3})
    reader.mutate_accounts_checked(
        StorageMutation(upserts=({**a, "quota": 7},), expected_revision=baseline),
        expected_items={"a": a},
    )
    assert reader.load_accounts() == [{**a, "quota": 7}, {**b, "quota": 3}]
    # Same-row conflicts must still abort the entire multi-row batch.
    with pytest.raises(StorageRevisionConflictError):
        reader.mutate_accounts_checked(
            StorageMutation(upserts=({**a, "quota": 6}, {**b, "quota": 6}), expected_revision=baseline),
            expected_items={"a": {**a, "quota": 7}, "b": b},
        )
    assert reader.load_accounts() == [{**a, "quota": 7}, {**b, "quota": 3}]


@pytest.mark.parametrize("missing", [False, True])
def test_checked_rotation_is_atomic_and_protects_missing_or_occupied_keys(databases, missing):
    reader, writer = databases
    old = {"access_token": "old", "quota": 8}
    reader.replace_accounts([old])
    if missing:
        writer.delete_account("old")
    else:
        writer.upsert_account({"access_token": "new", "quota": 5})
    before = reader.load_accounts()
    with pytest.raises(StorageRevisionConflictError):
        reader.mutate_accounts_checked(
            StorageMutation(upserts=({**old, "access_token": "new"},), delete_keys=("old",)),
            expected_items={"old": old, "new": None},
        )
    assert reader.load_accounts() == before


@pytest.mark.parametrize("expected", [{}, {"other": None}, {"a": {"access_token": "other"}}])
def test_checked_mutation_requires_complete_correct_baselines(databases, expected):
    reader, _ = databases
    with pytest.raises(ValueError):
        reader.mutate_accounts_checked(StorageMutation(upserts=({"access_token": "a"},)), expected_items=expected)
    assert reader.load_accounts() == []
