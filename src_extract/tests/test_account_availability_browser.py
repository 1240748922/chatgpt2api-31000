"""Real shipped modules, with synthetic APIs and no upstream requests."""
import time

import pytest

from test_account_import_browser import browser, origin, ui, pw, ROOT
from dashboard_fixtures import dashboard_payload


def inventory():
    return dict(
        counts=dict(ready=8000, unknown=400, expiring=20, expired=30, quarantined=5, invalid=2, disabled=1),
        generation_candidates=7900, edit_candidates=7800, refresh_candidates=420, needs_credentials=37,
        quota_unknown=80, upload_limited=100, snapshot_age_seconds=0,
        sampled_at="2026-09-24T00:00:00+00:00", note="就绪预评估，不等于实时空闲槽位。",
        maintenance=dict(mode="normal", policy="performance", batch_size=2, active_images=300,
                         sampled_at=time.time(), reasons=["性能正常，允许小批同步"],
                         signals=dict(database_ms=15, writer_wait_ms=3, upstream_ms=900)),
    )


@pytest.mark.parametrize("width", [1440, 768])
def test_readiness_renders_refreshes_and_does_not_break_import_or_navigation(ui, width):
    ui.page.set_viewport_size(dict(width=width, height=1050))
    ui.availability = inventory()
    ui.dashboard = dashboard_payload()
    ui.open("access_token")
    ui.upload([dict(access_token="synthetic-file-at", refresh_token="opaque-rt")])
    pw.expect(ui.textarea).to_have_value("synthetic-file-at")
    pw.expect(ui.dialog.get_by_role("button", name="开始导入", exact=True)).to_be_enabled()
    ui.close()
    panel = ui.page.get_by_role("region", name="账号可用性", exact=True)
    pw.expect(panel).to_be_visible()
    assert panel.locator("[data-readiness-state]").count() == 7
    pw.expect(panel.locator('[data-readiness-state="ready"]')).to_contain_text("8,000")
    pw.expect(panel).to_contain_text("活跃生图 300")
    pw.expect(panel).to_contain_text("正常同步")
    size = panel.evaluate("el => ({width:el.clientWidth, scroll:el.scrollWidth})")
    assert size["scroll"] <= size["width"] + 1, size
    ui.page.screenshot(path=str(ROOT / f".runtime/account-availability-{width}.png"), full_page=True)
    ui.availability["counts"]["ready"] = 0
    ui.availability["maintenance"].update(mode="paused", batch_size=0, reasons=["数据库操作耗时较高"])
    panel.get_by_role("button", name="刷新账号可用性").click()
    pw.expect(panel).to_contain_text("数据库操作耗时较高")
    pw.expect(panel.locator('[data-readiness-state="ready"]')).to_contain_text("0")
    # Same SPA instance, not reloads: the shared runtime and route bindings
    # must survive repeated transitions in either direction.
    for _ in range(2):
        if width < 1024:
            ui.page.get_by_role("button", name="打开导航", exact=True).click()
        ui.page.locator('#app-sidebar-navigation a[href="#/"]').click()
        pw.expect(ui.page).to_have_url(ui.origin + "/#/")
        pw.expect(ui.page.get_by_text("当前并发", exact=True)).to_be_visible()
        pw.expect(panel).to_have_count(1)
        if width < 1024:
            ui.page.get_by_role("button", name="打开导航", exact=True).click()
        ui.page.locator('#app-sidebar-navigation a[href="#/accounts"]').click()
        pw.expect(ui.page).to_have_url(ui.origin + "/#/accounts")
        pw.expect(panel).to_have_count(1)
    assert not ui.errors, ui.errors


def test_availability_failure_keeps_last_sample_and_does_not_disable_import(ui):
    ui.availability = inventory()
    ui.open()
    ui.close()
    panel = ui.page.get_by_role("region", name="账号可用性", exact=True)
    pw.expect(panel).to_contain_text("8,000")
    ui.availability_status = 503
    panel.get_by_role("button", name="刷新账号可用性").click()
    pw.expect(panel.get_by_role("status")).to_contain_text("不影响导入")
    pw.expect(panel).to_contain_text("8,000")
    # HTTP failure is expected; JS/module/render errors are not.
    ui.errors[:] = [error for error in ui.errors if "503" not in error]
    ui.reopen()
    ui.textarea.fill("synthetic-rt")
    start = ui.dialog.get_by_role("button", name="开始导入", exact=True)
    pw.expect(start).to_be_enabled()
    start.click()
    pw.expect(ui.dialog.get_by_label("选择导入任务")).to_have_value("synthetic-job-1")
    ui.close()


def test_account_row_explains_unknown_expiry(ui):
    from services.account_view import account_row
    from test_account_auth_quarantine import row
    ui.availability = inventory()
    ui.accounts = [account_row(row("synthetic-opaque-at", management_id="synthetic-ready-id", email="ready@example.test"),
                               available=True, unlimited_quota=False)]
    ui.page.goto(ui.origin + "/#/accounts")
    pw.expect(ui.page.get_by_text("AT 有效期未知", exact=True)).to_be_visible()
    ui.page.get_by_label("凭据状态", exact=True).hover()
    detail = ui.page.locator("[data-account-readiness-detail]")
    pw.expect(detail).to_be_visible()
    pw.expect(detail).to_contain_text("有效期未知")
    assert not ui.errors, ui.errors
