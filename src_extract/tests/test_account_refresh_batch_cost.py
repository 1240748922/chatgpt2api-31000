"""Small renewal batches must not materialize the entire credential pool."""
from threading import Lock

import pytest

from services.account_service import AccountService
from test_account_auth_quarantine import jwt, row, service_factory


@pytest.mark.parametrize("force", [False, True])
def test_exchange_preserves_rotated_credentials_without_listing_pool(service_factory, monkeypatch, force):
    old, new = jwt(-3600), jwt(7 * 86400)
    service = service_factory([row(old, refresh_token="synthetic-rt")])
    account_id = service.get_account(old)["management_id"]
    monkeypatch.setattr(service, "list_accounts", lambda: pytest.fail("copied entire pool"))
    monkeypatch.setattr(service, "_request_access_token_refresh", lambda *a, **kw: {
        "access_token": new, "refresh_token": "rotated-rt"})
    operation = service.refresh_access_tokens if force else service.renew_expiring_access_tokens
    result = operation([old, old])
    assert result == dict(refreshed=1, skipped=0, updated_ids=[account_id], removed_ids=[], errors=[])
    saved = service.storage.load_accounts()
    assert len(saved) == 1
    assert saved[0]["access_token"] == new and saved[0]["refresh_token"] == "rotated-rt"
    assert saved[0]["management_id"] == account_id


@pytest.mark.parametrize("force", [False, True])
def test_exchange_still_excludes_accounts_deleted_before_final_snapshot(service_factory, monkeypatch, force):
    old, new = jwt(-3600), jwt(7 * 86400)
    service = service_factory([row(old, refresh_token="synthetic-rt")])
    account_id = service.get_account(old)["management_id"]
    method = "force_refresh_access_token" if force else "ensure_access_token"
    original = getattr(service, method)

    def refresh_then_delete(*a, **kw):
        token = original(*a, **kw)
        service.delete_accounts([token], return_items=False)
        return token

    monkeypatch.setattr(service, method, refresh_then_delete)
    monkeypatch.setattr(service, "list_accounts", lambda: pytest.fail("copied entire pool"))
    monkeypatch.setattr(service, "_request_access_token_refresh", lambda *a, **kw: {
        "access_token": new, "refresh_token": "rotated-rt"})
    operation = service.refresh_access_tokens if force else service.renew_expiring_access_tokens
    result = operation([old])
    assert result["refreshed"] == 1
    assert result["errors"] == []
    assert result["updated_ids"] == [] and result["removed_ids"] == [account_id]
    assert service.storage.load_accounts() == []


@pytest.mark.parametrize("pool_size", [5000, 10000, 20000])
def test_targeted_existence_check_reads_only_ids_and_refreshes_snapshot(monkeypatch, pool_size):
    class IdOnly:
        def __init__(self, value):
            self.value = value

        def get(self, key):
            assert key == "management_id"
            return self.value

    service = object.__new__(AccountService)
    service._lock = Lock()
    service._accounts = {}
    refreshes = []

    def refresh():
        refreshes.append(1)
        service._accounts = {str(i): IdOnly(f" ID-{i} ") for i in range(pool_size)}

    monkeypatch.setattr(service, "_refresh_accounts_snapshot_if_stale", refresh)
    last_id = f"id-{pool_size - 1}"
    assert service._existing_account_ids([" ID-0 ", last_id, "missing"]) == {"id-0", last_id}
    assert refreshes == [1]
    assert service._existing_account_ids([]) == set()
    assert refreshes == [1]


def test_existence_check_stops_after_all_targets_found(monkeypatch):
    class Untouched:
        def get(self, key):
            pytest.fail("scanned unrelated tail after finding every requested ID")

    service = object.__new__(AccountService)
    service._lock = Lock()
    service._accounts = {
        "first": {"management_id": "ID-FIRST"},
        "last": {"management_id": "id-last"},
        "tail": Untouched(),
    }
    monkeypatch.setattr(service, "_refresh_accounts_snapshot_if_stale", lambda: None)
    assert service._existing_account_ids(["id-first", "ID-FIRST", " ID-LAST "]) == {
        "id-first", "id-last",
    }
