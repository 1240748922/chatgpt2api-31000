from types import SimpleNamespace

import pytest

from services import account_replenishment_service as module


def test_status_poll_is_read_only_and_preserves_current_launch_logs(monkeypatch):
    state = {
        "running": True,
        "last_result": {"reason": "starting", "stdout_tail": "正在检查启动环境"},
        "last_metrics": {"current_available": 10},
        "log_history": [],
    }
    service = module.AccountReplenishmentService(repository=SimpleNamespace(load=lambda: state))
    service._last_metrics = {"current_available": 1}  # Stale replica-local state.
    monkeypatch.setattr(module.account_service, "evaluate_account_pool", lambda **kw: pytest.fail("full pool scan during log poll"))
    status = service.status()
    assert status["running"] is True
    assert status["last_result"]["stdout_tail"] == "正在检查启动环境"
    assert status["current_metrics"]["current_available"] == 10


def test_proxy_test_uses_registration_preflight_and_redacts_credentials(monkeypatch):
    seen = []

    def preflight(proxy, *, proxy_attempts):
        seen.append((proxy, proxy_attempts))
        return {
            "ok": True,
            "proxy": proxy,
            "checks": [{"name": "chatgpt-login", "ok": True, "status": 200}],
            "error": None,
        }

    monkeypatch.setattr(
        "sms_tool.registration_preflight.registration_network_preflight_report",
        preflight,
    )
    service = module.AccountReplenishmentService(repository=SimpleNamespace(load=lambda: {}))
    result = service.test_registration_proxy(
        "socks5://user:secret@example.test:1080",
        "",
    )

    assert result["ok"] is True
    assert seen == [("socks5://user:secret@example.test:1080", 2)]
    assert result["results"][0]["proxy"] == "socks5://***@example.test:1080"
    assert result["results"][0]["checks"][0]["name"] == "chatgpt-login"
