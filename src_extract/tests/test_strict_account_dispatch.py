"""Production admission and independent-process coordination, no upstream I/O."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from test_account_auth_quarantine import jwt, row, service_factory
from services.account_service import AccountService, ImageAccountSelectionError
from services.credential_coordinator import CredentialBusy, CredentialGate, generation, resource


@pytest.fixture(autouse=True)
def strict(monkeypatch):
    monkeypatch.setenv("CHATGPT2API_STRICT_IMAGE_CREDENTIALS", "1")


def test_production_default_is_strict(monkeypatch):
    monkeypatch.delenv("CHATGPT2API_STRICT_IMAGE_CREDENTIALS")
    assert AccountService._strict_admission()


@pytest.mark.parametrize("token", ["opaque", jwt(-10), jwt(10), jwt(300)])
def test_unknown_expired_short_never_trigger_frontend_oauth(service_factory, token):
    service = service_factory([row(token, refresh_token="private-rt")])
    with pytest.raises(ImageAccountSelectionError):
        service.get_available_access_token(deadline_monotonic=time.monotonic() + 400)
    assert not service._image_inflight


def test_unknown_is_background_recoverable_but_quota_sync_is_not_expiry_proof(service_factory, monkeypatch):
    service = service_factory([row("opaque", refresh_token="private-rt", last_remote_check_result="ok")])
    assert service.list_expiring_access_tokens() == ["opaque"]
    fresh = jwt(7200)
    monkeypatch.setattr(service, "_request_access_token_refresh", lambda *a, **kw: {"access_token": fresh, "refresh_token": "new-rt"})
    service.renew_expiring_access_tokens(["opaque"])
    leased = service.get_available_access_token()
    assert leased == fresh
    service.release_image_slot(leased)


def test_image_on_one_instance_blocks_manual_and_background_refresh_on_other(service_factory, monkeypatch):
    token = jwt(7200)
    first = service_factory([row(token, refresh_token="private-rt")])
    second = service_factory(url=first.storage.database_url)
    lease = first.get_available_access_token()
    called = []
    monkeypatch.setattr(second, "_request_access_token_refresh", lambda *a, **kw: called.append(1))
    with pytest.raises(CredentialBusy):
        second.force_refresh_access_token(token, raise_on_error=True)
    second.renew_expiring_access_tokens([token])
    assert called == []
    first.release_image_slot(lease)
    monkeypatch.setattr(second, "_request_access_token_refresh", lambda *a, **kw: {"access_token": jwt(14400), "refresh_token": "new-rt"})
    assert second.force_refresh_access_token(token, raise_on_error=True) == jwt(14400)


def test_refresh_blocks_other_instance_and_is_single_send(service_factory, monkeypatch):
    old, new = jwt(7200), jwt(14400)
    first = service_factory([row(old, refresh_token="private-rt")])
    second = service_factory(url=first.storage.database_url)
    entered, finish = Event(), Event()
    calls = []
    def exchange(*a, **kw):
        calls.append(1)
        entered.set()
        assert finish.wait(5)
        return {"access_token": new, "refresh_token": "new-rt"}
    monkeypatch.setattr(first, "_request_access_token_refresh", exchange)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(first.force_refresh_access_token, old, raise_on_error=True)
        try:
            assert entered.wait(2)
            with pytest.raises(ImageAccountSelectionError):
                second.get_available_access_token()
            with pytest.raises(CredentialBusy):
                second.force_refresh_access_token(old, raise_on_error=True)
        finally:
            finish.set()
        assert future.result() == new
    assert calls == [1]
    assert first.readiness_summary()["counts"]["ready"] == 1


@pytest.mark.parametrize("failure", ["network", "commit"])
def test_ambiguous_refresh_is_not_replayed_after_restart(service_factory, monkeypatch, failure):
    old = jwt(7200)
    first = service_factory([row(old, refresh_token="private-rt")])
    def exchange(*a, **kw):
        if failure == "network":
            raise TimeoutError("synthetic timeout")
        return {"access_token": jwt(14400), "refresh_token": "new-rt"}
    monkeypatch.setattr(first, "_request_access_token_refresh", exchange)
    if failure == "commit":
        monkeypatch.setattr(first, "_apply_refreshed_tokens", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("synthetic DB failure")))
    with pytest.raises((TimeoutError, RuntimeError)):
        first.force_refresh_access_token(old, raise_on_error=True)
    restarted = service_factory(url=first.storage.database_url)
    with pytest.raises(CredentialBusy, match="uncertain"):
        restarted.force_refresh_access_token(old, raise_on_error=True)
    with pytest.raises(ImageAccountSelectionError):
        restarted.get_available_access_token()


def test_old_snapshot_cannot_send_after_other_instance_rotates(service_factory, monkeypatch):
    old, new = jwt(7200), jwt(14400)
    first = service_factory([row(old, refresh_token="private-rt")])
    second = service_factory(url=first.storage.database_url)
    monkeypatch.setattr(first, "_request_access_token_refresh", lambda *a, **kw: {"access_token": new, "refresh_token": "new-rt"})
    first.force_refresh_access_token(old, raise_on_error=True)
    try:
        lease = second.get_available_access_token()
    except ImageAccountSelectionError:
        pass
    else:
        assert lease == new  # A fast background snapshot may already see the new generation.
        second.release_image_slot(lease)
    assert not second._image_inflight


def test_exact_concurrent_leases_and_idempotent_shared_release(service_factory, monkeypatch):
    from types import SimpleNamespace
    import services.account_service as module
    monkeypatch.setattr(module, "config", SimpleNamespace(image_account_concurrency=2, image_request_timeout_secs=300))
    token = jwt(7200)
    first = service_factory([row(token, refresh_token="private-rt")])
    second = service_factory(url=first.storage.database_url)
    a = first.get_available_access_token()
    b = second.get_available_access_token()
    with pytest.raises(ImageAccountSelectionError):
        first.get_available_access_token()
    b.release()
    b.release()
    with pytest.raises(CredentialBusy):
        second._credential_coordinator.begin_refresh(second._accounts[token])
    a.release()
    ticket = second._credential_coordinator.begin_refresh(second._accounts[token])
    second._credential_coordinator.finish_refresh(ticket, "idle")
    first.release_image_slot(a)
    second.release_image_slot(b)


def test_result_and_interruption_both_release_shared_lease(service_factory):
    token = jwt(7200)
    service = service_factory([row(token, refresh_token="private-rt")])
    lease = service.get_available_access_token()
    service.mark_image_result(lease, True)
    ticket = service._credential_coordinator.begin_refresh(service._accounts[token])
    service._credential_coordinator.finish_refresh(ticket, "idle")
    lease = service.get_available_access_token()
    service.release_image_slot(lease)
    ticket = service._credential_coordinator.begin_refresh(service._accounts[token])
    service._credential_coordinator.finish_refresh(ticket, "idle")


def test_upload_cooldown_and_quota_filters_survive(service_factory):
    from services.account_capabilities import record_upload_throttle
    token = jwt(7200)
    item = row(token)
    record_upload_throttle(item, 60, time.time())
    service = service_factory([item])
    with pytest.raises(ImageAccountSelectionError):
        service.get_available_access_token(requires_file_upload=True)
    lease = service.get_available_access_token()
    service.release_image_slot(lease)


def test_rt_import_send_fence_has_no_credentials_and_no_ttl_replay(service_factory):
    service = service_factory([])
    coordinator = service._credential_coordinator
    item = {"refresh_token": "private-rt"}
    ticket = coordinator.begin_refresh(item, imported=True)
    with pytest.raises(CredentialBusy):
        coordinator.begin_refresh(item, imported=True)
    coordinator.finish_refresh(ticket, "done")
    with pytest.raises(CredentialBusy, match="already_exchanged"):
        coordinator.begin_refresh(item, imported=True)
    with coordinator.Session() as session:
        stored = session.query(CredentialGate).one()
        assert "private-rt" not in stored.data + stored.key


def test_refresh_guard_transaction_does_not_occupy_connection_during_http(service_factory):
    token = jwt(7200)
    service = service_factory([row(token, refresh_token="private-rt")])
    coordinator = service._credential_coordinator
    ticket = coordinator.begin_refresh(service._accounts[token])
    # A completely unrelated SQLite write succeeds while the OAuth owner is out
    # on the network. This would block under a transaction spanning HTTP.
    with coordinator.edit("synthetic-independent") as (_, data, _):
        data["example"] = 1
    coordinator.finish_refresh(ticket, "idle")


def test_expired_crashed_image_lease_reclaimed_but_sent_refresh_not_reclaimed(service_factory):
    token = jwt(7200)
    service = service_factory([row(token, refresh_token="private-rt")])
    coordinator = service._credential_coordinator
    key = resource(service._accounts[token])
    with coordinator.edit(key) as (_, data, now):
        data["leases"]["dead-worker"] = now - 1
    ticket = coordinator.begin_refresh(service._accounts[token])
    with pytest.raises(CredentialBusy):
        coordinator.begin_refresh(service._accounts[token])
    coordinator.finish_refresh(ticket, "uncertain")



def test_shared_lease_time_survives_timing_collector(service_factory, monkeypatch):
    service = service_factory([row(jwt(7200))])
    original = service._credential_coordinator.acquire_image
    def delayed(*args, **kwargs):
        time.sleep(.015)
        return original(*args, **kwargs)
    monkeypatch.setattr(service._credential_coordinator, "acquire_image", delayed)
    token = service.get_available_access_token()
    metrics = service.get_image_selection_diagnostics()
    assert metrics["account_credential_lease_ms"] >= 10
    assert metrics["account_token_http_ms"] == 0
    service.release_image_slot(token)


def test_uncertain_refresh_is_visible_not_reported_as_available(service_factory):
    token = jwt(7200)
    service = service_factory([row(token, refresh_token="private-rt")])
    ticket = service._credential_coordinator.begin_refresh(service._accounts[token])
    service._credential_coordinator.finish_refresh(ticket, "uncertain")
    summary = service.readiness_summary()
    assert summary["counts"]["uncertain"] == 1
    assert summary["generation_candidates"] == 0
    assert summary["needs_credentials"] == 1


@pytest.mark.parametrize("state", ["refreshing", "uncertain"])
def test_ready_quota_excludes_shared_refresh_gate_on_another_instance(service_factory, state):
    blocked, ready = jwt(7200), jwt(7201)
    first = service_factory([row(blocked, quota=999, refresh_token="private-rt"), row(ready, quota=7)])
    second = service_factory(url=first.storage.database_url)
    ticket = first._credential_coordinator.begin_refresh(first._accounts[blocked])
    if state == "uncertain":
        first._credential_coordinator.finish_refresh(ticket, "uncertain")
    summary = second.readiness_summary()
    assert summary["counts"][state] == 1
    assert summary["generation_candidates"] == summary["edit_candidates"] == 1
    assert summary["generation_quota"] == summary["edit_quota"] == dict(
        known_remaining=7, known_accounts=1, unknown_accounts=0, unlimited_accounts=0)
    if state == "refreshing":
        first._credential_coordinator.finish_refresh(ticket, "idle")


from test_account_ingest import ingest


def test_strict_rt_ingestion_checkpoint_and_duplicate_import(ingest, monkeypatch):
    service, db = ingest
    calls = []
    def exchange(*a, **kw):
        calls.append(1)
        return {"access_token": jwt(7200), "refresh_token": "rotated-private-rt"}
    monkeypatch.setattr(service.accounts, "_request_access_token_refresh", exchange)
    job = service.submit([{"refresh_token": "private-rt"}])
    assert service.claim("refresh", "worker") == job["id"]
    result = service.save_batch(job["id"], "worker", refresh=True)
    assert result["added"] == 1 and result["refresh_failed"] == 0
    # Importing the current RT of an existing managed account reuses it locally.
    duplicate = service.submit([{"refresh_token": "rotated-private-rt"}])
    assert service.claim("refresh", "again") == duplicate["id"]
    result = service.save_batch(duplicate["id"], "again", refresh=True)
    assert result["added"] == 0 and calls == [1]
    # Never replay the superseded RT, even in a separate job.
    with pytest.raises(CredentialBusy, match="already_exchanged"):
        service.accounts._credential_coordinator.begin_refresh({"refresh_token": "private-rt"}, imported=True)
