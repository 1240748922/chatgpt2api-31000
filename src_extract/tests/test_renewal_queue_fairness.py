"""Replay historical queue starvation with synthetic credentials, no upstream."""
from threading import Lock

import pytest

from services.account_service import AccountService
from test_account_auth_quarantine import jwt, row, now, service_factory
from test_account_maintenance_cadence import watcher
from test_refresh_token_reused import mock_oauth


REJECTION = "oauth_refresh_http_401: refresh_token_reused: historical rejection"


def ordered_candidates(monkeypatch, ordinary_count, historical_count):
    """Use the real selector on memory-only rows, avoiding a large test DB."""
    ordinary = [jwt(36000 + i) for i in range(ordinary_count)]
    historical = [jwt(-86400 + i) for i in range(historical_count)]
    accounts = [row(t, refresh_token="ordinary-rt") for t in ordinary]
    accounts += [row(t, refresh_token="historical-rt", last_token_refresh_error=REJECTION)
                 for t in historical]
    service = object.__new__(AccountService)
    service._accounts = {a["access_token"]: a for a in reversed(accounts)}
    service._lock = Lock()
    scans = []
    monkeypatch.setattr(service, "_refresh_accounts_snapshot_if_stale", lambda: scans.append(1))
    result = service.list_expiring_access_tokens()
    assert scans == [1]  # Same one snapshot refresh as before, not per batch/account.
    assert len(result) == len(set(result)) == ordinary_count + historical_count
    ordinary_set, historical_set = set(ordinary), set(historical)
    assert [t for t in result if t in ordinary_set] == ordinary
    assert [t for t in result if t in historical_set] == historical
    return result, ordinary, historical


def test_observed_backlog_is_not_behind_all_461_early_renewals(monkeypatch):
    result, ordinary, historical = ordered_candidates(monkeypatch, 461, 15302)
    assert result[:8] == ordinary[:3] + historical[:1] + ordinary[3:6] + historical[1:2]
    assert result.index(historical[0]) == 3  # Was 461, although these ATs expired days ago.
    assert result[-1] == historical[-1]


@pytest.mark.parametrize("normal,legacy", [(0, 0), (9, 0), (0, 9), (1, 9), (9, 1), (6, 2)])
def test_empty_or_uneven_groups_never_duplicate_or_drop(monkeypatch, normal, legacy):
    result, ordinary, historical = ordered_candidates(monkeypatch, normal, legacy)
    if historical:
        assert result.index(historical[0]) == min(3, normal)


def test_reduced_watcher_preserves_share_across_single_account_turns(watcher, monkeypatch):
    result, ordinary, historical = ordered_candidates(monkeypatch, 461, 10)
    watcher.pending["expiry"] = result
    watcher.policy.update(mode="reduced", batch_size=1)
    assert watcher.run(turns=8) == [30] * 8
    attempted = [token for kind, _, batch in watcher.calls if kind == "renew" for token in batch]
    assert attempted == ordinary[:3] + historical[:1] + ordinary[3:6] + historical[1:2]
    assert watcher.scans["expiry"] == 1  # No rediscovery/sort resets the share.


def test_terminal_recheck_reduces_real_inventory_without_dropping_valid_at(service_factory, monkeypatch):
    monkeypatch.setenv("CHATGPT2API_STRICT_IMAGE_CREDENTIALS", "1")
    ordinary = [jwt(36000 + i) for i in range(12)]
    historical = [jwt(-86400 + i) for i in range(3)]
    service = service_factory([
        *[row(t, refresh_token=f"ordinary-rt-{i}") for i, t in enumerate(ordinary)],
        *[row(t, refresh_token=f"historical-rt-{i}", last_token_refresh_error=REJECTION)
          for i, t in enumerate(historical)],
    ])
    assert service.readiness_summary()["refresh_unverified"] == 3
    calls = mock_oauth(monkeypatch, service)
    first = service.list_expiring_access_tokens()[:4]
    assert first == ordinary[:3] + historical[:1]
    for token in first:
        result = service.renew_expiring_access_tokens([token])
        assert len(result["errors"]) == 1
    assert len(calls) == 4
    # The UI projection intentionally caches for 5s; inspect the next sample,
    # not a snapshot captured before the four background attempts.
    cached_at, cached_summary = service._readiness_summary_cache
    service._readiness_summary_cache = (cached_at - 6, cached_summary)
    summary = service.readiness_summary()
    assert summary["refresh_unverified"] == 2
    assert summary["needs_credentials"] == 1
    assert summary["counts"]["ready"] == 12
    assert len(service.list_tokens()) == 15  # No automatic deletion.
    for token in first:
        account = service.get_account(token)
        assert account["refresh_token_invalid_at"] and account["quota"] == 8
        assert token not in service.list_expiring_access_tokens()
    assert service.ensure_access_token(ordinary[0]) == ordinary[0]
    assert len(calls) == 4  # Confirmed bad RT is not re-exchanged.


def test_share_does_not_bypass_disabled_pending_backoff_or_terminal(service_factory, monkeypatch):
    monkeypatch.setenv("CHATGPT2API_STRICT_IMAGE_CREDENTIALS", "1")
    rows = [row(jwt(-5000 + i), refresh_token=f"rt-{i}", last_token_refresh_error=REJECTION,
                **extra) for i, extra in enumerate([
                    {"status": "禁用"}, {"last_remote_check_result": "pending", "pending_auth_scope": "image"},
                    {"last_token_refresh_error_at": now()}, {"refresh_token_invalid_at": now()}, {},
                ])]
    service = service_factory(rows)
    assert service.list_expiring_access_tokens() == [rows[-1]["access_token"]]


def test_historical_text_does_not_prevent_successful_recovery(service_factory, monkeypatch):
    monkeypatch.setenv("CHATGPT2API_STRICT_IMAGE_CREDENTIALS", "1")
    old, fresh = jwt(-86400), jwt(7 * 86400)
    service = service_factory([row(old, refresh_token="replacement-rt", last_token_refresh_error=REJECTION)])
    assert service.readiness_summary()["refresh_unverified"] == 1
    monkeypatch.setattr(service, "_request_access_token_refresh",
                        lambda *a, **kw: {"access_token": fresh, "refresh_token": "rotated-rt"})
    result = service.renew_expiring_access_tokens(service.list_expiring_access_tokens())
    assert result["refreshed"] == 1 and not result["errors"]
    cached_at, cached_summary = service._readiness_summary_cache
    service._readiness_summary_cache = (cached_at - 6, cached_summary)
    summary = service.readiness_summary()
    assert summary["refresh_unverified"] == 0
    assert summary["needs_credentials"] == 0
    assert summary["counts"]["ready"] == 1
    assert not service.get_account(fresh)["refresh_token_invalid_at"]
    leased = service.get_available_access_token()
    assert leased == fresh
    service.release_image_slot(leased)
