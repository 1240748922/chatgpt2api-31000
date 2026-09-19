"""Deterministic cross-replica import visibility and empty-pool waiting."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Condition, Lock
from types import SimpleNamespace

import pytest

import services.account_service as accounts_module
from services.account_service import AccountService, ImageAccountSelectionError
from test_image_account_selection import _account
from test_image_recovery import Clock


@pytest.fixture
def replica(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(accounts_module, "time", clock)
    pending = []
    class QueuedThread:
        def __init__(self, *, target, **kw):
            self.target = target
        def start(self):
            pending.append(self.target)
    monkeypatch.setattr(accounts_module, "Thread", QueuedThread)
    class AdvancingCondition(Condition):
        def wait(self, timeout=None):
            self.release()
            try:
                clock.sleep(timeout)
                for target in list(pending):
                    pending.remove(target)
                    target()
            finally:
                self.acquire()
    service = AccountService.__new__(AccountService)
    service._lock = Lock()
    service._write_lock = Lock()
    service._write_baseline = None
    service._image_slot_condition = AdvancingCondition(service._lock)
    service._account_snapshot_refresh_lock = Lock()
    service._account_snapshot_refresh_dispatch_lock = Lock()
    service._account_snapshot_refresh_scheduled = False
    service._account_snapshot_checked_at = clock.now
    service._accounts_revision = "old"
    service._accounts = OrderedDict()
    service._persisted_accounts = {}
    service._image_inflight = {}
    service._token_aliases = {}
    service._index = service._image_index = service._image_shard_index = 0
    service._image_shard_count = 1
    service._IMAGE_POOL_WAIT_SECONDS = 2
    state = SimpleNamespace(revision="new", accounts=OrderedDict(), reads=0, checks=0)
    def revision(collection):
        assert collection == "accounts"
        state.checks += 1
        return state.revision
    def read():
        state.reads += 1
        return state.accounts.copy(), state.revision, False
    service.storage = SimpleNamespace(get_collection_revision=revision)
    monkeypatch.setattr(service, "_read_accounts_snapshot", read)
    monkeypatch.setattr(service, "ensure_access_token", lambda token, **kw: token)
    return SimpleNamespace(service=service, clock=clock, state=state, pending=pending)


@pytest.mark.parametrize("all_limited", [False, True])
def test_import_on_another_replica_is_seen_before_empty_pool_wait_expires(replica, all_limited):
    service, state = replica.service, replica.state
    if all_limited:
        item = _account("exhausted", unknown=False)
        item["status"] = "限流"
        service._accounts["exhausted"] = item
    state.accounts["imported"] = _account("imported", quota=8, unknown=False)
    token = service.get_available_access_token(deadline_monotonic=replica.clock.now + 10)
    assert token == "imported"
    assert replica.clock.now < 1002
    assert state.reads == 1
    service.release_image_slot(token)
    assert service._image_inflight == {}


def test_empty_pool_revision_checks_are_throttled_without_full_reload(replica):
    replica.state.revision = "old"
    with pytest.raises(ImageAccountSelectionError) as caught:
        replica.service.get_available_access_token(deadline_monotonic=replica.clock.now + 10)
    assert caught.value.code == "no_available_account"
    assert 1 <= replica.state.checks <= 4
    assert replica.state.reads == 0
    assert replica.clock.now == 1002


def test_populated_fast_path_keeps_normal_snapshot_ttl(replica):
    replica.service._accounts["ready"] = _account("ready", quota=8, unknown=False)
    assert replica.service.get_available_access_token() == "ready"
    assert replica.state.checks == replica.state.reads == 0
    assert replica.pending == []


def test_non_database_backend_does_not_reload_every_half_second(replica, monkeypatch):
    replica.service.storage = SimpleNamespace(get_backend_info=lambda: {"type": "git"})
    replica.clock.sleep(2)
    assert not replica.service._refresh_accounts_snapshot_if_stale(max_age_seconds=0.5)
    assert replica.state.reads == 0


def test_empty_pool_does_not_extend_request_deadline(replica):
    with pytest.raises(ImageAccountSelectionError) as caught:
        replica.service.get_available_access_token(deadline_monotonic=1000.25)
    assert caught.value.code == "task_interrupted"
    assert replica.clock.now == 1000.25


def test_refresh_does_not_bypass_upload_cooldown(replica):
    limited = _account("upload-blocked", quota=8, unknown=False)
    limited["file_upload_blocked_until"] = replica.clock.now + 3600
    replica.state.accounts["upload-blocked"] = limited
    with pytest.raises(ImageAccountSelectionError):
        replica.service.get_available_access_token(requires_file_upload=True)
    assert replica.service.get_available_access_token() == "upload-blocked"


def test_changed_pool_schedules_only_one_reload_for_many_waiters(replica):
    replica.clock.sleep(5)
    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(lambda _: replica.service._refresh_accounts_snapshot_if_stale(
            allow_full_reload=False), range(100)))
    assert len(replica.pending) == 1
    assert replica.state.checks == 0  # revision I/O now runs in the background too
    replica.pending.pop()()
    assert replica.state.reads == 1
    assert replica.service._account_snapshot_refresh_scheduled is False


def test_single_account_write_does_not_hide_imports_on_other_replicas(replica):
    service = replica.service
    used = _account("used", quota=7, unknown=False)
    service._accounts["used"] = used
    service._persisted_accounts["used"] = {**used, "quota": 8}
    service._image_result_persist_lock = Lock()
    service._image_result_persist_scheduled = True
    service._image_result_persist_dirty = True
    service._image_result_persist_pending = {"used": dict(used)}
    replica.state.accounts.update(used=used, imported=_account("imported", quota=8, unknown=False))
    service.storage.mutate_accounts = lambda mutation: SimpleNamespace(revision=replica.state.revision)
    service._flush_image_result_persistence()
    # The write revision includes an import this replica has never loaded.
    # Selecting after its TTL must discover it, even though the write succeeded.
    replica.clock.sleep(5)
    assert service._refresh_accounts_snapshot_if_stale()
    assert service.get_available_access_token(excluded_tokens={"used"}) == "imported"


@pytest.mark.parametrize("remote_has_pending_write", [False, True])
def test_refresh_keeps_unflushed_upload_cooldown_without_double_counting(replica, remote_has_pending_write):
    service = replica.service
    base = _account("used", quota=8, unknown=False)
    base.update(fail=0, success=0)
    local = {**base, "fail": 1, "file_upload_blocked_until": 5000}
    service._accounts["used"] = local
    service._persisted_accounts["used"] = base
    replica.state.accounts["used"] = deepcopy(local if remote_has_pending_write else base)
    replica.state.accounts["used"]["last_remote_checked_at"] = "2026-09-17T10:00:00+00:00"
    replica.clock.sleep(5)
    assert service._refresh_accounts_snapshot_if_stale()
    assert service._accounts["used"]["file_upload_blocked_until"] == 5000
    assert service._accounts["used"]["fail"] == 1
    assert service._accounts["used"]["last_remote_checked_at"] == "2026-09-17T10:00:00+00:00"


def test_snapshot_refresh_does_not_resurrect_remotely_removed_account(replica):
    base = _account("removed", quota=8, unknown=False)
    replica.service._accounts["removed"] = {**base, "file_upload_blocked_until": 5000}
    replica.service._persisted_accounts["removed"] = base
    replica.clock.sleep(5)
    assert replica.service._refresh_accounts_snapshot_if_stale()
    assert "removed" not in replica.service._accounts


def test_snapshot_refresh_does_not_transfer_old_credential_failure(replica):
    base = _account("used", quota=8, unknown=False)
    replica.service._accounts["used"] = {**base, "status": "异常"}
    replica.service._persisted_accounts["used"] = base
    replica.state.accounts["used"] = {**base, "refresh_token": "replacement"}
    replica.clock.sleep(5)
    assert replica.service._refresh_accounts_snapshot_if_stale()
    assert replica.service._accounts["used"]["status"] == "正常"
