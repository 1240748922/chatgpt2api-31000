"""Registration uses its proxy without turning it into an account binding."""
import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from sms_tool import registration_preflight, storage
from sms_tool.commands.registration import persist_registration_result
from sms_tool.config import current_config_data
from sms_tool.registration_handlers import RegistrationEmailWorkflow
from sms_tool.registration_state import RegistrationStateMachine
from sms_tool.session_builder import build_session_file


PROXY = "http://synthetic-region-US-sid-FINAL001-t-5:p%40ss%3Aword@proxy.example.test:8080"


@pytest.mark.parametrize("proxy", [
    PROXY, "socks5h://user:p%2Bass@[2001:db8::10]:1080", "http://proxy.example.test:8080", "",
])
def test_session_builder_does_not_export_registration_proxy(proxy):
    session = build_session_file({"access_token": "synthetic-at", "proxy": proxy})
    assert "proxy" not in session
    assert session["access_token"] == "synthetic-at"
    assert session["refresh_token_status"] == "no_rt"


@pytest.mark.parametrize("data", [
    {"proxy": PROXY, "registration_proxy": "http://old.example.test:8080",
     "response": {"proxy": "http://other.example.test:8080"}},
    {"registration_proxy": PROXY}, {"response": {"proxy": PROXY}},
    {"proxy": "  " + PROXY + "  "}, {},
])
def test_session_builder_ignores_legacy_registration_proxy_fields(data):
    session = build_session_file({"access_token": "synthetic-at", **data})
    assert "proxy" not in session and "registration_proxy" not in session


def test_workflow_keeps_post_preflight_proxy_for_registration_only(monkeypatch):
    initial = "socks5h://synthetic-region-US-sid-INITIAL1-t-5:p%40ss%3Aword@proxy.example.test:8080"
    # Exercise the real scheme resolver while intercepting every network probe.
    monkeypatch.setattr(registration_preflight, "_proxy_scheme_reachable",
                        lambda proxy, _url: proxy.startswith("http://"))
    seen = []

    def preflight(*, proxy, proxy_attempts):
        seen.append(proxy)
        assert proxy_attempts == 2
        return {"ok": True, "proxy": PROXY}

    cfg = {"registration": {}, "chatgpt": {}}
    operations = SimpleNamespace(
        validate_config=lambda *_a, **_kw: None,
        _resolve_proxy_scheme=registration_preflight._resolve_proxy_scheme,
        registration_network_preflight=preflight,
        _ensure_mailbox_account=lambda _mailbox: SimpleNamespace(email="synthetic@example.test"),
        set_fingerprint_geo=lambda _country: None,
        _sanitize_text=lambda text: str(text or ""),
        _oauth_result_summary=lambda _result: {},
        _timing_summary=lambda: {},
        _mailbox_snapshot=lambda _mailbox: {},
        _retain_registration_checkpoint=lambda *_args: True,
    )
    monkeypatch.setattr("sms_tool.mailbox_service.MailboxService.create", lambda _cfg: object())
    monkeypatch.setattr("sms_tool.auth_headers.current_auth_fingerprint", lambda: {})
    monkeypatch.setattr("sms_tool.sentinel_quickjs.sentinel_version", lambda: "synthetic-test")
    workflow = RegistrationEmailWorkflow(RegistrationStateMachine(lambda *_: None),
                                         proxy=initial, config=cfg, operations=operations)
    monkeypatch.setattr(workflow, "_persist_checkpoint", lambda *_: None)
    monkeypatch.setattr(workflow, "_has_resume_checkpoint", lambda: False)
    monkeypatch.setattr(workflow, "_run_stage", lambda *_: None)
    workflow._bootstrap()
    workflow.runtime.access_token = "synthetic-at"
    workflow.runtime.success = True
    result = workflow.finalize()

    assert seen == [initial.replace("socks5h://", "http://", 1)]
    assert workflow.runtime.proxy == PROXY and workflow.runtime.proxy != initial
    assert "proxy" not in result
    assert "proxy" not in build_session_file(result)


def test_hot_persistence_does_not_bind_or_log_registration_proxy(tmp_path, capsys):
    def thaw(value):
        if isinstance(value, Mapping):
            return {key: thaw(item) for key, item in value.items()}
        if isinstance(value, list):
            return [thaw(item) for item in value]
        return value

    cfg = thaw(current_config_data())
    cfg["storage"] = {**cfg.get("storage", {}), "sqlite_path": str(tmp_path / "registration.sqlite3")}
    cfg["output"] = {**cfg.get("output", {}), "filename_pattern": "session_{email}_{timestamp}.json"}
    captured = []

    def upsert(data, *, json_path):
        assert "proxy" not in data
        captured.append(dict(data))
        return storage.upsert_account(data, json_path=json_path, runtime_config=cfg)

    ctx = SimpleNamespace(runtime_config=cfg, build_session_file=build_session_file, upsert_account=upsert)
    args = SimpleNamespace(registration_batch_id="proxy-test")
    data = {"success": True, "email": "synthetic@example.test", "access_token": "synthetic-at",
            "proxy": PROXY}
    result = persist_registration_result(args, data, tmp_path, ctx)

    assert result["status"] == "complete"
    assert result["session_saved"] == result["db_saved"] == 1
    session = json.loads(Path(result["session_path"]).read_text(encoding="utf-8"))
    assert "proxy" not in session and PROXY not in json.dumps(session)
    assert session["batch_id"] == "proxy-test"
    assert len(captured) == 1 and captured[0]["access_token"] == "synthetic-at"
    output = capsys.readouterr().out
    assert "Saved session:" in output
    assert PROXY not in output
    assert "p%40ss%3Aword" not in output
    assert "synthetic-at" not in output

    # A final batch report retries completed results without rewriting the session.
    assert persist_registration_result(args, data, tmp_path, ctx) == result
    assert len(captured) == 1
