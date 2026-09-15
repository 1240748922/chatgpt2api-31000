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
