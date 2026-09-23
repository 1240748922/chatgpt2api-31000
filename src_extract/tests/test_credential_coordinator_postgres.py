"""CI-only real PostgreSQL interleavings, in a dedicated disposable database."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from threading import Barrier
import time

import pytest
from sqlalchemy import text, make_url

from services.credential_coordinator import CredentialCoordinator, CredentialBusy, CredentialGate
from services.storage.database_storage import DatabaseStorageBackend


@pytest.fixture
def pair():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("requires disposable TEST_POSTGRES_URL")
    assert make_url(url).database == "credential_test", "Never run against an application database"
    store = DatabaseStorageBackend(url)
    with store.engine.begin() as connection:
        connection.execute(text("DELETE FROM credential_runtime_gates"))
        connection.execute(text("DELETE FROM accounts"))
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 3600}).encode()).decode().rstrip("=")
    account = {"access_token": f"e30.{payload}.synthetic", "refresh_token": "synthetic-rt", "status": "正常"}
    store.replace_accounts([account])
    yield CredentialCoordinator(store), CredentialCoordinator(DatabaseStorageBackend(url)), account


def test_postgres_refresh_vs_image_only_one_can_win(pair):
    a, b, account = pair
    barrier = Barrier(2)
    def image():
        barrier.wait()
        try:
            return a.acquire_image(account, minimum_validity=300, duration=400, limit=1)
        except CredentialBusy:
            return None
    def refresh():
        barrier.wait()
        try:
            return b.begin_refresh(account)
        except CredentialBusy:
            return None
    with ThreadPoolExecutor(2) as executor:
        x, y = executor.submit(image), executor.submit(refresh)
        results = [x.result(timeout=5), y.result(timeout=5)]
    assert sum(result is not None for result in results) == 1


def test_postgres_send_fence_survives_new_coordinator(pair):
    a, b, account = pair
    ticket = a.begin_refresh(account)
    with pytest.raises(CredentialBusy):
        b.begin_refresh(account)
    a.finish_refresh(ticket, "uncertain")
    restarted = CredentialCoordinator(a.storage)
    with pytest.raises(CredentialBusy, match="uncertain"):
        restarted.begin_refresh(account)


def test_postgres_no_connection_or_row_lock_held_during_network(pair):
    a, b, account = pair
    ticket = a.begin_refresh(account)
    with b.edit("unrelated-test-key") as (_, data, _):
        data["value"] = 1
    # Lease acquisition finishes quickly rather than blocking on OAuth.
    started = time.monotonic()
    with pytest.raises(CredentialBusy):
        b.acquire_image(account, minimum_validity=300, duration=400, limit=1)
    assert time.monotonic() - started < 2
    a.finish_refresh(ticket, "done")


def test_postgres_exact_release_and_authoritative_generation(pair):
    a, b, account = pair
    one = a.acquire_image(account, minimum_validity=300, duration=400, limit=2)
    two = b.acquire_image(account, minimum_validity=300, duration=400, limit=2)
    one.release()
    one.release()
    with pytest.raises(CredentialBusy):
        a.begin_refresh(account)
    two.release()
    a.storage.replace_accounts([{**account, "refresh_token": "new-rt"}])
    with pytest.raises(CredentialBusy, match="credential_changed"):
        b.acquire_image(account, minimum_validity=300, duration=400, limit=2)

