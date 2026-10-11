"""Exercise actual account editing in Chromium using synthetic APIs and proxy URLs.

ACCOUNTS_PROXY_UI_BASELINE=HEAD reproduces the edit-input regression.
"""
import os
import re
import subprocess
from urllib.parse import urlsplit

import pytest

from services.account_view import account_detail, account_row
from test_account_import_browser import ImportUI, ROOT, browser, origin, pw


FIRST = "http://synthetic-region-US-sid-FIRST001-t-5:p%40ss%3Aword@first.example.test:8080"
SECOND = "socks5h://synthetic-region-JP-sid-SECOND02-t-5:p%2Bass@second.example.test:1080"


class ProxyEditUI(ImportUI):
    def __init__(self, page, origin):
        self.writes, self.tested = [], []
        self.proxy_baseline = os.environ.get("ACCOUNTS_PROXY_UI_BASELINE")
        self.old_bundle = None
        self.records = {
            f"synthetic-{index}": dict(management_id=f"synthetic-{index}",
                email=f"synthetic-{index}@example.test", access_token=f"synthetic-{index}-at",
                proxy=proxy, type="free", status="正常", quota=8, source_type="web")
            for index, proxy in enumerate([FIRST, SECOND])
        }
        super().__init__(page, origin)
        self.update_rows()

    def update_rows(self):
        self.accounts = [account_row(row, available=True, unlimited_quota=False)
                         for row in self.records.values()]

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if request.url.startswith(self.origin + "/"):
            if self.proxy_baseline and path == "/assets/Accounts-CQrrBRkk.js":
                if self.old_bundle is None:
                    self.old_bundle = subprocess.check_output([
                        "git", "show", f"{self.proxy_baseline}:src_extract/web_dist{path}"], cwd=ROOT)
                route.fulfill(body=self.old_bundle, content_type="text/javascript")
                return
            if path.startswith("/api/accounts/synthetic-") and request.method == "GET":
                account = self.records[path.rsplit("/", 1)[1]]
                route.fulfill(json={"item": account_detail(account, available=True, unlimited_quota=False)})
                return
            if path == "/api/proxy/test" and request.method == "POST":
                self.tested.append(request.post_data_json["url"])
                route.fulfill(json={"result": {"ok": True, "latency_ms": 1, "status": 200}})
                return
            if path == "/api/accounts/update" and request.method == "POST":
                payload = request.post_data_json
                self.writes.append(payload)
                account = self.records[payload["id"]]
                account["proxy"] = payload["proxy"]
                self.update_rows()
                route.fulfill(json={"item": account_detail(account, available=True, unlimited_quota=False),
                                   "updated_ids": [payload["id"]], "removed_ids": [], "errors": [],
                                   "status_label": "已更新", "tone": "success", "message": "账号已更新",
                                   "summary_items": [], "events": []})
                return
        super().route(route)

    def open_edit(self, index=0):
        self.page.get_by_role("button", name="编辑", exact=True).nth(index).click()
        pw.expect(self.edit_dialog).to_be_visible()

    @property
    def edit_dialog(self):
        return self.page.get_by_role("dialog", name="编辑账号", exact=True)

    @property
    def proxy_input(self):
        return self.edit_dialog.get_by_placeholder("http://127.0.0.1:7890", exact=True)


@pytest.fixture
def proxy_ui(browser, origin):
    context = browser.new_context(viewport={"width": 1440, "height": 1050})
    ui = ProxyEditUI(context.new_page(), origin)
    ui.page.goto(origin + "/#/accounts")
    pw.expect(ui.page.get_by_role("button", name="编辑", exact=True)).to_have_count(2)
    yield ui
    context.close()


def test_edit_test_and_save_preserves_complete_proxy(proxy_ui):
    ui = proxy_ui
    ui.open_edit()
    pw.expect(ui.proxy_input).to_have_value(FIRST)
    pw.expect(ui.edit_dialog.get_by_text("http://first.example.test:8080", exact=True)).to_be_visible()
    ui.edit_dialog.get_by_role("button", name="测试当前代理", exact=True).click()
    confirmation = ui.page.get_by_role("dialog", name="确认测试账号代理", exact=True)
    confirmation.get_by_role("button", name="开始测试", exact=True).click()
    pw.expect(ui.page.get_by_text(re.compile("代理可用："))).to_be_visible()
    assert ui.tested == [FIRST]
    ui.edit_dialog.get_by_role("button", name="保存", exact=True).click()
    pw.expect(ui.edit_dialog).to_have_count(0)
    assert len(ui.writes) == 1
    assert ui.writes[0]["proxy"] == FIRST
    ui.open_edit(1)
    pw.expect(ui.proxy_input).to_have_value(SECOND)
    ui.edit_dialog.get_by_role("button", name="关闭", exact=True).click()
    pw.expect(ui.edit_dialog).to_have_count(0)
    assert len(ui.writes) == 1, "closing must not mutate the second account"
    assert not ui.errors, ui.errors


def test_editing_custom_proxy_is_saved_without_cross_account_leak(proxy_ui):
    ui = proxy_ui
    ui.open_edit()
    pw.expect(ui.proxy_input).to_have_value(FIRST)
    replacement = "http://replacement:synthetic@third.example.test:8080"
    ui.proxy_input.fill(replacement)
    ui.edit_dialog.get_by_role("button", name="保存", exact=True).click()
    pw.expect(ui.edit_dialog).to_have_count(0)
    assert ui.writes[0]["proxy"] == replacement
    ui.open_edit(1)
    pw.expect(ui.proxy_input).to_have_value(SECOND)
    ui.edit_dialog.get_by_role("button", name="关闭", exact=True).click()
    ui.open_edit()
    pw.expect(ui.proxy_input).to_have_value(replacement)
    assert not ui.errors, ui.errors
