"""Replay observed OAuth rejection offline; retain valid ATs and credential fences."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from test_account_auth_quarantine import jwt, now, row, service_factory
from services.account_credentials import project_upstream_credential_availability
from services.account_service import AccountService, OAuthRefreshError, TerminalRefreshTokenError


REUSED_MESSAGE = (
    "Your refresh token has already been used to generate a new access token. "
    "Please try signing in again."
)


@pytest.fixture(autouse=True)
def strict(monkeypatch):
    monkeypatch.setenv("CHATGPT2API_STRICT_IMAGE_CREDENTIALS", "1")


def mock_oauth(monkeypatch, service, *, status=401, code="refresh_token_reused",
               shape="nested", before_response=None, exception=None):
    from curl_cffi import requests

    payload = ({"error": {"code": code, "message": REUSED_MESSAGE}} if shape == "nested"
               else {"error": code, "error_description": REUSED_MESSAGE})
    calls = []

    class Session:
        def __init__(self, **kwargs): pass
        def close(self): pass

        def post(self, *args, **kwargs):
            calls.append(1)
            if before_response:
                before_response()
            if exception:
                raise exception
            return SimpleNamespace(status_code=status, text=json.dumps(payload), json=lambda: payload)

    monkeypatch.setattr(requests, "Session", Session)
    monkeypatch.setattr(service, "_request_access_token_refresh",
                        AccountService._request_access_token_refresh.__get__(service))
    return calls


@pytest.mark.parametrize("shape", ["nested", "oauth"])
@pytest.mark.parametrize("status", [400, 401])
def test_reused_response_is_terminal(service_factory, monkeypatch, shape, status):
    service = service_factory([])
    calls = mock_oauth(monkeypatch, service, status=status, shape=shape)
    with pytest.raises(TerminalRefreshTokenError) as error:
        service._request_access_token_refresh("synthetic-rt")
    assert error.value.error_code == "refresh_token_reused"
    assert calls == [1]


@pytest.mark.parametrize("lifetime", [-3600, 7200])
def test_background_reused_rt_stops_retrying_without_removing_valid_at(
        service_factory, monkeypatch, lifetime):
    token = jwt(lifetime)
    service = service_factory([row(token, refresh_token="synthetic-rt")])
    calls = mock_oauth(monkeypatch, service)
    result = service.renew_expiring_access_tokens([token])
    assert result["refreshed"] == 0 and len(result["errors"]) == 1
    item = service.get_account(token)
    assert item["refresh_token_invalid_at"]
    assert "refresh_token_reused" in item["last_token_refresh_error"]
    assert item["status"] == "正常" and item["quota"] == 8
    assert service.list_expiring_access_tokens() == []
    summary = service.readiness_summary()
    assert summary["refresh_candidates"] == 0
    assert summary["needs_credentials"] == (1 if lifetime < 0 else 0)
    projected = project_upstream_credential_availability(
        token, item["refresh_token"], refresh_confirmed_invalid=True)
    assert projected.status == ("unavailable" if lifetime < 0 else "usable")
    preview = service.preview_auto_remove_accounts(
        remove_invalid=False, remove_rate_limited=False, remove_unusable_credentials=True)
    assert preview["credentials_unavailable"] == (1 if lifetime < 0 else 0)

    # Expiring backoff and restarting another replica must not revive this RT.
    service.update_account(token, {"last_token_refresh_error_at": (
        datetime.now(timezone.utc) - timedelta(days=1)).isoformat()})
    other = service_factory(url=service.storage.database_url)
    assert other.list_expiring_access_tokens() == []
    for operation in (service.renew_expiring_access_tokens, other.renew_expiring_access_tokens,
                      service.refresh_access_tokens, other.refresh_access_tokens):
        again = operation([token])
        assert again["errors"][0]["code"] == "refresh_token_invalid"
    assert calls == [1]
    if lifetime > 0:
        assert other.ensure_access_token(token) == token
        leased = other.get_available_access_token()
        assert leased == token
        other.release_image_slot(leased)


@pytest.mark.parametrize("status,code", [
    (408, "refresh_token_reused"), (429, "refresh_token_reused"),
    (500, "refresh_token_reused"), (503, "refresh_token_reused"),
    (401, "unknown_oauth_error"),
])
def test_transient_or_unknown_rejection_is_not_permanent(service_factory, monkeypatch, status, code):
    token = jwt(-3600)
    service = service_factory([row(token, refresh_token="synthetic-rt")])
    mock_oauth(monkeypatch, service, status=status, code=code)
    with pytest.raises(OAuthRefreshError) as error:
        service.ensure_access_token(token, raise_on_error=True)
    assert not isinstance(error.value, TerminalRefreshTokenError)
    assert not service.get_account(token)["refresh_token_invalid_at"]


def test_network_timeout_retains_uncertain_fence(service_factory, monkeypatch):
    token = jwt(-3600)
    service = service_factory([row(token, refresh_token="synthetic-rt")])
    calls = mock_oauth(monkeypatch, service, exception=TimeoutError("synthetic timeout"))
    service.renew_expiring_access_tokens([token])
    assert not service.get_account(token)["refresh_token_invalid_at"]
    service.update_account(token, {"last_token_refresh_error_at": None})
    service.renew_expiring_access_tokens([token])
    assert calls == [1]  # A timeout may already have rotated RT. Never replay it.


@pytest.mark.parametrize("writer", ["same_instance", "other_instance"])
def test_stale_reused_response_does_not_poison_replacement(service_factory, monkeypatch, writer):
    old, new = jwt(-3600), jwt(7 * 86400)
    service = service_factory([row(old, refresh_token="old-synthetic-rt")])
    other = service if writer == "same_instance" else service_factory(url=service.storage.database_url)
    calls = mock_oauth(monkeypatch, service, before_response=lambda: other.update_account(
        old, {"access_token": new, "refresh_token": "new-synthetic-rt"}))
    service.renew_expiring_access_tokens([old])
    item = service.get_account(new)
    assert item and item["refresh_token"] == "new-synthetic-rt"
    assert not item["refresh_token_invalid_at"]
    assert not item["last_token_refresh_error"]
    assert calls == [1]
    leased = service.get_available_access_token()
    assert leased == new
    service.release_image_slot(leased)


@pytest.mark.parametrize("path", ["single", "bulk", "import"])
def test_replacing_rt_clears_old_error_and_backoff(service_factory, path):
    token = jwt(-3600)
    service = service_factory([row(
        token, refresh_token="old-synthetic-rt", refresh_token_invalid_at=now(),
        last_token_refresh_error="oauth_refresh_http_401: refresh_token_reused: old rejection",
        last_token_refresh_error_at=now())])
    updates = {"refresh_token": "new-synthetic-rt"}
    if path == "single":
        service.update_account(token, updates)
    elif path == "bulk":
        service.update_accounts([token], updates)
    else:
        service._add_account_payloads([{"access_token": token, **updates}], return_items=False)
    item = service.get_account(token)
    assert not item["refresh_token_invalid_at"]
    assert not item["last_token_refresh_error"]
    assert not item["last_token_refresh_error_at"]
    assert service.list_expiring_access_tokens() == [token]


def test_duplicate_rt_and_backup_restore_preserve_lifecycle(service_factory):
    token = jwt(-3600)
    error_at = now()
    initial = row(token, refresh_token="synthetic-rt", refresh_token_invalid_at=error_at,
                  last_token_refresh_error="oauth_refresh_http_401: refresh_token_reused",
                  last_token_refresh_error_at=error_at)
    service = service_factory([initial])
    service._add_account_payloads([{"access_token": token, "refresh_token": "synthetic-rt"}])
    assert service.get_account(token)["refresh_token_invalid_at"] == error_at
    assert service.get_account(token)["last_token_refresh_error_at"] == error_at
    restored = service_factory([])
    restored._add_account_payloads([initial], preserve_lifecycle=True)
    assert restored.get_account(token)["refresh_token_invalid_at"] == error_at
    assert restored.get_account(token)["last_token_refresh_error_at"] == error_at


def test_historical_text_alone_does_not_invalidate_current_rt(service_factory):
    token = jwt(-3600)
    service = service_factory([row(
        token, refresh_token="possibly-replaced-rt",
        last_token_refresh_error="oauth_refresh_http_401: refresh_token_reused: old rejection")])
    assert not service.get_account(token)["refresh_token_invalid_at"]
    assert service.readiness_summary()["refresh_candidates"] == 1
