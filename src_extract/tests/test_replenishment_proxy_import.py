"""Registration -> import -> authoritative DB -> egress, only synthetic inputs."""
import io
import json
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from services import account_replenishment_service as module
from services.account_service import AccountService
from services.account_view import account_detail, account_row
from services.application_database import dispose_database_engine
from services.proxy_management_service import ProxyManagementService
from services.proxy_service import ProxySettingsStore
from services.storage.database_storage import DatabaseStorageBackend
from sms_tool.session_builder import build_session_file


FIRST = "http://synthetic-region-US-sid-FIRST001-t-5:p%40ss%3Aword@first.example.test:8080"
SECOND = "socks5h://synthetic-region-JP-sid-SECOND02-t-5:p%2Bass@second.example.test:1080"
DEFAULT = "http://default:synthetic@default.example.test:8080"


@pytest.fixture
def account_store(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "accounts.sqlite").as_posix()
    backend = DatabaseStorageBackend(url)
    monkeypatch.setattr(AccountService, "_load_cumulative_total", lambda _self: 0)
    monkeypatch.setattr(AccountService, "_save_cumulative_total", lambda _self: None)
    settings = {}
    config = SimpleNamespace(get=lambda: settings, get_proxy_runtime_settings=lambda: {})
    assignment = ProxyManagementService(config_store=config)
    accounts = AccountService(backend, proxy_reference_mutation=assignment.mutate_assignment_references)
    monkeypatch.setattr(module, "account_service", accounts)
    yield accounts, backend, ProxySettingsStore(config_store=config), settings
    accounts._image_result_persist_executor.shutdown(wait=True)
    accounts._image_failure_schedule_executor.shutdown(wait=True)
    dispose_database_engine(url)


def session_path(tmp_path, name, **data):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def replenishment():
    return module.AccountReplenishmentService(repository=SimpleNamespace(load=lambda: {}))


def test_generated_sessions_import_without_registration_proxy(account_store, tmp_path):
    accounts, backend, proxies, _ = account_store
    paths = [session_path(tmp_path, f"session_{index}.json", **{
        **build_session_file({"email": f"synthetic-{index}@example.test",
                              "access_token": f"synthetic-{index}-at", "proxy": proxy}),
        "batch_id": "test-batch",
    }) for index, proxy in enumerate([FIRST, SECOND])]
    result = replenishment()._import_sessions(paths + paths, batch_id="test-batch")

    assert result["imported"] == 2 and result["skipped"] == 0
    assert result["proxy_assigned"] is False and result["proxy_policy"] == "inherit"
    assert len(result["paths"]) == 2
    assert result["_access_tokens"] == ["synthetic-0-at", "synthetic-1-at"]
    persisted = backend.load_accounts_snapshot().items
    assert len(persisted) == 2
    for account in persisted:
        assert account["proxy"] == ""
        assert accounts.get_account(account["access_token"])["proxy"] == ""
        assert proxies.get_profile(account=account, upstream=True).proxy_source == "direct"
        row = account_row(account, available=True, unlimited_quota=False)
        detail = account_detail(account, available=True, unlimited_quota=False)
        assert row["proxy_mode"] == "inherit" and row["proxy"] == ""
        assert detail["configuration"]["proxy"] == ""
        assert FIRST not in json.dumps(account) and SECOND not in json.dumps(account)


@pytest.mark.parametrize("reference", [FIRST, SECOND, "direct", "global", "group:missing", "custom", ""])
def test_old_session_proxy_is_ignored_and_global_egress_still_applies(account_store, tmp_path, reference):
    _, backend, proxies, settings = account_store
    settings["proxy"] = DEFAULT
    before = deepcopy(settings)
    path = session_path(tmp_path, "session_old.json", access_token="synthetic-at",
                        proxy=reference, proxy_group_id="missing", proxy_mode="custom",
                        registration_proxy=FIRST, account_proxy=SECOND)
    original = path.read_bytes()
    result = replenishment()._import_sessions([path])
    assert result["imported"] == 1 and result["proxy_assigned"] is False
    account, = backend.load_accounts_snapshot().items
    assert account["proxy"] == ""
    assert all(key not in account for key in ["proxy_group_id", "proxy_mode", "registration_proxy", "account_proxy"])
    profile = proxies.get_profile(account=account, upstream=True)
    assert profile.proxy_url == DEFAULT and profile.proxy_source == "default"
    assert settings == before and path.read_bytes() == original


def test_existing_manual_assignment_is_preserved_on_automatic_reimport(account_store, tmp_path):
    accounts, backend, proxies, settings = account_store
    settings["proxy"] = DEFAULT
    accounts.add_account_items([{"access_token": "synthetic-at", "proxy": FIRST}], return_items=False)
    path = session_path(tmp_path, "session_repeat.json", access_token="synthetic-at", proxy=SECOND,
                        proxy_group_id="missing", email="synthetic@example.test")
    result = replenishment()._import_sessions([path])
    assert result["imported"] == 0 and result["skipped"] == 1 and result["proxy_assigned"] is False
    account, = backend.load_accounts_snapshot().items
    assert account["proxy"] == FIRST and account["email"] == "synthetic@example.test"
    assert proxies.get_profile(account=account).proxy_source == "account"
    assert proxies.get_profile(account=account).proxy_url == FIRST


def test_manual_import_keeps_explicit_proxy_and_account_group_inheritance(account_store, tmp_path):
    accounts, backend, proxies, settings = account_store
    settings.update(proxy=DEFAULT, account_groups=[{"id": "synthetic-group", "proxy": SECOND}])
    accounts.add_account_items([{"access_token": "manual-at", "proxy": FIRST}], return_items=False)
    path = session_path(tmp_path, "session_group.json", access_token="group-at", group_id="synthetic-group",
                        proxy=FIRST, proxy_group_id="missing")
    replenishment()._import_sessions([path])
    persisted = {row["access_token"]: row for row in backend.load_accounts_snapshot().items}
    manual = proxies.get_profile(account=persisted["manual-at"])
    grouped = proxies.get_profile(account=persisted["group-at"])
    assert manual.proxy_url == FIRST and manual.proxy_source == "account"
    assert persisted["group-at"]["proxy"] == "" and persisted["group-at"]["group_id"] == "synthetic-group"
    assert grouped.proxy_url == SECOND and grouped.proxy_source == "account_group"


def test_invalid_or_foreign_sessions_cannot_enter_pool(account_store, tmp_path):
    _, backend, _, _ = account_store
    paths = [
        session_path(tmp_path, "session_ok.json", access_token="synthetic-at", batch_id="this-batch"),
        session_path(tmp_path, "session_foreign.json", access_token="foreign-at", proxy=FIRST, batch_id="other"),
        session_path(tmp_path, "session_no_at.json", proxy=SECOND, batch_id="this-batch"),
    ]
    broken = tmp_path / "session_broken.json"
    broken.write_text("{broken-json", encoding="utf-8")
    paths.append(broken)
    service = replenishment()
    result = service._import_sessions(paths, batch_id="this-batch")
    assert result["imported"] == 1 and result["proxy_assigned"] is False
    account, = backend.load_accounts_snapshot().items
    assert account["access_token"] == "synthetic-at" and account["proxy"] == ""
    empty = service._import_sessions(paths, batch_id="missing")
    assert empty["imported"] == 0 and empty["proxy_assigned"] is False
    assert empty["_access_tokens"] == [] and empty["proxy_policy"] == "inherit"


def test_run_once_imports_and_syncs_via_default_without_reading_registration_proxy(account_store, tmp_path, monkeypatch):
    accounts, backend, proxies, settings = account_store
    settings["proxy"] = DEFAULT
    state = {}

    @contextmanager
    def edit():
        yield state

    service = module.AccountReplenishmentService(repository=SimpleNamespace(load=lambda: state, edit=edit))
    run_settings = dict(enabled=True, minimum_available=1, target_available=1, max_batch_size=1,
                        cooldown_seconds=1, interval_seconds=30, launch_timeout_seconds=300,
                        import_new_sessions=True, workers=1)
    monkeypatch.setattr(service, "_config_snapshot", lambda: run_settings)
    monkeypatch.setattr(service, "_registration_configuration_error", lambda _: None)
    monkeypatch.setattr(service, "_pool_metrics", lambda **_: {"current_available": 0})
    monkeypatch.setattr(accounts, "evaluate_account_pool", lambda **_: {"current_available": 0})
    monkeypatch.setattr(service, "provider_config", lambda: pytest.fail("registration proxy is not an account assignment"))
    monkeypatch.setattr(service, "_resolve_tool_paths", lambda _: (tmp_path, tmp_path, tmp_path / "synthetic.py", tmp_path / "python"))
    calls = []

    def popen(command, **kwargs):
        calls.append(command)
        assert kwargs["cwd"] == str(tmp_path)
        batch = command[command.index("--registration-batch-id") + 1]
        path = session_path(tmp_path, "session_run.json", batch_id=batch,
                            access_token="synthetic-at", proxy=FIRST, proxy_group_id="missing")
        return SimpleNamespace(stdout=io.StringIO(f"[*] Saved session: {path}\n"), wait=lambda **_: 0)

    sync_calls = []

    def sync(tokens):
        sync_calls.append(tokens)
        account, = backend.load_accounts_snapshot().items
        profile = proxies.get_profile(account=account, upstream=True)
        assert account["proxy"] == "" and profile.proxy_url == DEFAULT
        assert profile.proxy_source == "default"
        return {"synced": 1, "errors": []}

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(accounts, "sync_accounts_and_quota", sync)
    result = service._run_once(force=True, requested_count=1)
    assert result["ok"] is True and result["reason"] == "replenished"
    assert len(calls) == 1 and sync_calls == [["synthetic-at"]]
    assert result["import"]["imported"] == 1 and result["import"]["proxy_assigned"] is False
    assert result["quota_refresh"] == {"synced": 1, "errors": 0, "attempts": 1}
    assert "_access_tokens" not in result["import"]
    assert state["running"] is False and state["log_history"][-1]["import"]["proxy_policy"] == "inherit"
