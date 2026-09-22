"""Shipped page -> real FastAPI preview/selection -> synthetic in-memory delete."""
from collections import OrderedDict
from threading import RLock
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import system
from services.account_service import AccountService
from services.account_view import account_row
from services import account_service as account_module
from cleanup_fixtures import cleanup_accounts
from test_account_import_browser import ImportUI, ROOT, browser, origin, pw


class CleanupUI(ImportUI):
    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if request.url.startswith(self.origin + "/") and path == "/api/accounts":
            rows = [account_row(a, available=False, unlimited_quota=False) for a in self.service._accounts.values()]
            route.fulfill(json=dict(items=rows, total=len(rows), all_total=len(rows), page=1, page_size=20))
            return
        if request.url.startswith(self.origin + "/") and path.startswith("/api/settings/account-cleanup"):
            self.payloads.append((path, request.post_data_json))
            if getattr(self, "fail_cleanup", False) and not path.endswith("/preview"):
                route.fulfill(status=503, json={"detail": "synthetic cleanup unavailable"})
                return
            response = self.client.post(path, json=request.post_data_json, headers={"Authorization": "test-only"})
            route.fulfill(status=response.status_code, json=response.json())
            return
        super().route(route)

    def open_cleanup(self):
        self.page.get_by_role("button", name="批量处理", exact=True).click()
        self.page.get_by_role("menuitem", name="删除异常账号", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="删除异常账号", exact=True)
        pw.expect(dialog).to_be_visible()
        return dialog


@pytest.fixture
def cleanup_ui(browser, origin, monkeypatch):
    service = AccountService.__new__(AccountService)
    service._lock = RLock()
    service._accounts = OrderedDict((a["access_token"], a) for a in cleanup_accounts())
    deleted = []
    def delete(tokens, **_):
        for token in tokens:
            if token in service._accounts:
                deleted.append(service._accounts.pop(token)["email"])
        return {"removed": len(tokens)}
    monkeypatch.setattr(service, "_refresh_accounts_snapshot_if_stale", lambda: None)
    monkeypatch.setattr(service, "delete_accounts", delete)
    monkeypatch.setattr(account_module.log_service, "add", lambda *a, **kw: None)
    monkeypatch.setattr(system, "account_service", service)
    monkeypatch.setattr(system, "require_admin", lambda _: None)
    app = FastAPI()
    app.include_router(system.create_router("test"))
    context = browser.new_context(viewport=dict(width=1440, height=1050))
    with TestClient(app) as client:
        ui = CleanupUI(context.new_page(), origin)
        ui.service, ui.client, ui.deleted, ui.payloads = service, client, deleted, []
        yield ui
    context.close()


@pytest.mark.parametrize("action", ["cancel", "confirm", "recovered"])
def test_abnormal_cleanup_preview_and_execution_match_status(cleanup_ui, action):
    ui = cleanup_ui
    ui.page.goto(ui.origin + "/#/accounts")
    row = ui.page.get_by_role("row").filter(has_text="expired-no-rt@example.test")
    pw.expect(row.get_by_text("异常", exact=True)).to_be_visible()
    dialog = ui.open_cleanup()
    pw.expect(dialog.get_by_text("符合条件：4 个账号", exact=True)).to_be_visible()
    expected = {name + "@example.test" for name in [
        "expired-no-rt", "remote-invalid", "both-invalid", "stored-abnormal"]}
    for email in expected:
        pw.expect(dialog.get_by_text(email, exact=True)).to_be_visible()
    for name in ["recoverable", "healthy", "quota-only", "timeout-only", "disabled"]:
        pw.expect(dialog.get_by_text(name + "@example.test", exact=True)).to_have_count(0)
    if action == "cancel":
        dialog.get_by_role("button", name="取消", exact=True).click()
        assert not ui.deleted
        assert len(ui.service._accounts) == 9
    else:
        if action == "recovered":
            for a in ui.service._accounts.values():
                if a["name"] == "expired-no-rt":
                    a["refresh_token"] = "synthetic-recovered-during-preview"
            expected.remove("expired-no-rt@example.test")
        else:
            ui.page.screenshot(path=str(ROOT / ".runtime/cleanup-abnormal-fixed.png"))
        dialog.get_by_role("button", name="确认删除全部 4 个", exact=True).click()
        pw.expect(ui.page.get_by_text(f"已删除 {len(expected)} 个账号", exact=True)).to_be_visible()
        assert set(ui.deleted) == expected
        assert len(ui.service._accounts) == 9 - len(expected)
        for email in expected:
            pw.expect(ui.page.get_by_role("row").filter(has_text=email)).to_have_count(0)
    pw.expect(dialog).to_have_count(0)
    assert all(payload["remove_unusable_credentials"] is True for _, payload in ui.payloads)
    assert not ui.errors, ui.errors


def test_projected_abnormal_accounts_do_not_produce_empty_preview(cleanup_ui):
    ui = cleanup_ui
    ui.service._accounts.pop("synthetic-stored-abnormal")
    ui.page.goto(ui.origin + "/#/accounts")
    pw.expect(ui.page.get_by_text("expired-no-rt@example.test", exact=True)).to_be_visible()
    dialog = ui.open_cleanup()
    pw.expect(dialog.get_by_text("符合条件：3 个账号", exact=True)).to_be_visible()
    pw.expect(dialog.get_by_role("button", name="确认删除全部 3 个", exact=True)).to_be_enabled()
    dialog.get_by_role("button", name="取消", exact=True).click()
    assert not ui.deleted and not ui.errors


def test_cleanup_response_error_is_reported_without_broken_callback(cleanup_ui):
    ui = cleanup_ui
    ui.fail_cleanup = True
    ui.page.goto(ui.origin + "/#/accounts")
    pw.expect(ui.page.get_by_text("expired-no-rt@example.test", exact=True)).to_be_visible()
    dialog = ui.open_cleanup()
    dialog.get_by_role("button", name="确认删除全部 4 个", exact=True).click()
    pw.expect(ui.page.get_by_text("账号清理失败", exact=False)).to_be_visible()
    assert not ui.deleted
    assert len(ui.service._accounts) == 9
    # Chromium reports the deliberately injected HTTP failure to the console.
    # Keep every other error visible, especially an unhandled callback TypeError.
    unexpected = [error for error in ui.errors if error !=
                  "Failed to load resource: the server responded with a status of 503 (Service Unavailable)"]
    assert not unexpected, unexpected
