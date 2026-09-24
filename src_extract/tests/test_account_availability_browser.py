"""Real shipped modules, with synthetic APIs and no upstream requests."""
import time

import pytest

from test_account_import_browser import browser, origin, ui, pw, ROOT
from dashboard_fixtures import dashboard_payload


def inventory():
    return dict(
        counts=dict(ready=8000, unknown=400, expiring=20, expired=30, quarantined=5, invalid=2, disabled=1, refreshing=1, uncertain=0),
        generation_candidates=7900, edit_candidates=7800, refresh_candidates=420, refresh_unverified=12345, needs_credentials=37,
        generation_quota=dict(known_remaining=24000, known_accounts=7817, unknown_accounts=80, unlimited_accounts=3),
        edit_quota=dict(known_remaining=23000, known_accounts=7718, unknown_accounts=80, unlimited_accounts=2),
        quota_unknown=80, upload_limited=100, snapshot_age_seconds=0,
        sampled_at="2026-09-24T00:00:00+00:00", note="就绪预评估，不等于实时空闲槽位。",
        maintenance=dict(mode="normal", policy="performance", batch_size=2, active_images=300,
                         sampled_at=time.time(), reasons=["性能正常，允许小批同步"],
                         signals=dict(database_ms=15, writer_wait_ms=3, upstream_ms=900)),
        maintenance_progress=dict(available=True, owner=True, instance="app0", sampled_at=time.time(),
                                  started_at=time.time()-3600, state="checking", active=1,
                                  batch=dict(kind="renewal", total=2, active=1, completed=1, queued=0),
                                  totals=dict(completed=12000, succeeded=11970, failed=20, skipped=10)),
        cleanup_policy=dict(auto_remove_invalid_accounts=False, renewal_failure_auto_delete=False),
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
    # Only four summary cards; diagnosis lives in a bounded native dialog.
    assert panel.locator("[data-availability-metric]").count() == 4
    pw.expect(panel.locator('[data-ready-quota="generation"]')).to_have_text("就绪额度 24,000 *")
    pw.expect(panel.locator('[data-ready-quota="edits"]')).to_have_text("就绪额度 23,000 *")
    ui.page.screenshot(path=str(ROOT / f".runtime/account-availability-compact-{width}.png"), full_page=True)
    panel.get_by_role("button", name="详情", exact=True).click()
    pw.expect(panel.locator('[data-maintenance-count="active"]')).to_contain_text("1")
    pw.expect(panel).to_contain_text("调度上限每批 2 个")
    pw.expect(panel.locator('.av-label-value').filter(has_text="活跃生图")).to_contain_text("300")
    pw.expect(panel).to_contain_text("允许同步")
    ui.page.screenshot(path=str(ROOT / f".runtime/account-maintenance-progress-{width}.png"), full_page=True)
    panel.get_by_role("tab", name="凭据与额度", exact=True).click()
    pw.expect(panel.get_by_role("region", name="凭据恢复分类")).to_contain_text("12,345")
    assert panel.locator("[data-readiness-state]").count() == 9
    pw.expect(panel.locator('[data-readiness-state="ready"]')).to_contain_text("8,000")
    pw.expect(panel.locator('[data-ready-quota-detail="generation"]')).to_contain_text("额度未知 80 个 · 无限额套餐 3 个")
    pw.expect(panel.locator('[data-ready-quota-detail="edits"]')).to_contain_text("23,000")
    size = panel.evaluate("el => ({width:el.clientWidth, scroll:el.scrollWidth})")
    assert size["scroll"] <= size["width"] + 1, size
    ui.page.screenshot(path=str(ROOT / f".runtime/account-availability-{width}.png"), full_page=True)
    ui.availability["counts"]["ready"] = 0
    ui.availability["maintenance"].update(mode="paused", batch_size=0, reasons=["数据库操作耗时较高"])
    panel.get_by_role("button", name="关闭可用性详情").click()
    panel.get_by_role("button", name="刷新账号可用性").click()
    panel.get_by_role("button", name="详情", exact=True).click()
    panel.get_by_role("tab", name="同步进度", exact=True).click()
    pw.expect(panel).to_contain_text("数据库操作耗时较高")
    panel.get_by_role("tab", name="凭据与额度", exact=True).click()
    pw.expect(panel.locator('[data-readiness-state="ready"]')).to_contain_text("0")
    panel.get_by_role("button", name="关闭可用性详情").click()
    # Same SPA instance, not reloads: the shared runtime and route bindings
    # must survive repeated transitions in either direction.
    for _ in range(2):
        if width < 1024:
            ui.page.get_by_role("button", name="打开导航", exact=True).click()
        ui.page.locator('#app-sidebar-navigation a[href="#/"]').click()
        pw.expect(ui.page).to_have_url(ui.origin + "/#/")
        pw.expect(ui.page.get_by_text("当前并发", exact=True)).to_be_visible()
        pw.expect(panel).to_have_count(1)
        pw.expect(panel.locator('[data-ready-quota="generation"]')).to_have_text("就绪额度 24,000 *")
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
    pw.expect(panel).to_contain_text("7,900")
    pw.expect(panel.locator('[data-ready-quota="generation"]')).to_have_text("就绪额度 24,000 *")
    ui.availability_status = 503
    panel.get_by_role("button", name="刷新账号可用性").click()
    pw.expect(panel.get_by_role("status")).to_contain_text("不影响导入")
    pw.expect(panel).to_contain_text("7,900")
    # HTTP failure is expected; JS/module/render errors are not.
    ui.errors[:] = [error for error in ui.errors if "503" not in error]
    ui.reopen()
    ui.textarea.fill("synthetic-rt")
    start = ui.dialog.get_by_role("button", name="开始导入", exact=True)
    pw.expect(start).to_be_enabled()
    start.click()
    pw.expect(ui.dialog.get_by_label("选择导入任务")).to_have_value("synthetic-job-1")
    ui.close()


def test_ready_quota_old_api_unknown_then_zero_and_large_numbers_do_not_overflow(ui):
    ui.page.set_viewport_size(dict(width=390, height=844))
    ui.availability = inventory()
    ui.availability.pop("generation_quota")
    ui.availability.pop("edit_quota")
    ui.open()
    ui.close()
    panel = ui.page.get_by_role("region", name="账号可用性", exact=True)
    pw.expect(panel.locator('[data-ready-quota="generation"]')).to_have_text("就绪额度 --")
    ui.availability["generation_quota"] = dict(known_remaining=0, known_accounts=0, unknown_accounts=0, unlimited_accounts=0)
    ui.availability["edit_quota"] = dict(known_remaining=123456789, known_accounts=10000, unknown_accounts=0, unlimited_accounts=2)
    panel.get_by_role("button", name="刷新账号可用性").click()
    pw.expect(panel.locator('[data-ready-quota="generation"]')).to_have_text("就绪额度 0")
    pw.expect(panel.locator('[data-ready-quota="edits"]')).to_have_text("就绪额度 123,456,789 *")
    for node in [panel, *panel.locator("[data-availability-metric]").all()]:
        size = node.evaluate("el => ({width:el.clientWidth, scroll:el.scrollWidth})")
        assert size["scroll"] <= size["width"] + 1, size
    ui.page.screenshot(path=str(ROOT / ".runtime/account-availability-quota-390.png"), full_page=True)
    panel.get_by_role("button", name="详情", exact=True).click()
    pw.expect(panel.get_by_role("dialog")).to_be_visible()
    ui.page.keyboard.press("Escape")
    pw.expect(panel.get_by_role("dialog")).not_to_be_visible()
    assert not ui.errors, ui.errors


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


def test_live_progress_polls_only_small_endpoint_and_stops_when_closed(ui):
    ui.availability = inventory()
    ui.open()
    ui.close()
    panel = ui.page.get_by_role("region", name="账号可用性", exact=True)
    panel.get_by_role("button", name="详情", exact=True).click()
    pw.expect(panel.locator('[data-maintenance-count="active"] strong')).to_have_text("1")
    full_calls = ui.availability_calls
    calls = ui.maintenance_calls
    progress = ui.availability["maintenance_progress"]
    progress.update(active=0, batch=None, state="waiting", sampled_at=time.time()+.01,
                    next_check_at=time.time()+30)
    progress["totals"].update(completed=12001, succeeded=11971)
    pw.expect(panel.locator('[data-maintenance-count="active"] strong')).to_have_text("0", timeout=5000)
    pw.expect(panel.locator('[data-maintenance-count="succeeded"] strong')).to_have_text("11,971")
    assert ui.maintenance_calls > calls
    assert ui.availability_calls == full_calls, "live progress must not trigger another full-pool summary"
    ui.page.keyboard.press("Escape")
    calls = ui.maintenance_calls
    ui.page.wait_for_timeout(3200)
    assert ui.maintenance_calls == calls
    assert not ui.errors, ui.errors


@pytest.mark.parametrize("width,dark", [(1440, False), (768, False), (390, False), (390, True)])
def test_detail_tabs_bounded_layout_large_counts_and_close(ui, width, dark):
    ui.page.set_viewport_size(dict(width=width, height=844))
    ui.availability = inventory()
    ui.availability["maintenance_progress"]["totals"].update(completed=123456789, succeeded=123456000)
    ui.open()
    ui.close()
    if dark:
        ui.page.evaluate("document.documentElement.dataset.theme='dark'")
    panel = ui.page.get_by_role("region", name="账号可用性", exact=True)
    panel.get_by_role("button", name="详情", exact=True).click()
    dialog = panel.get_by_role("dialog")
    for tab in ["同步进度", "凭据与额度", "处理规则"]:
        panel.get_by_role("tab", name=tab, exact=True).click()
        assert dialog.get_by_role("tabpanel").count() == 1
        bounds = dialog.bounding_box()
        assert bounds["y"] >= 0 and bounds["y"]+bounds["height"] <= 845, bounds
        for node in [dialog, dialog.locator('.av-dialog-body'), *dialog.locator('[role=tabpanel]:not([hidden])').all()]:
            size = node.evaluate("el => ({width:el.clientWidth,scroll:el.scrollWidth})")
            assert size["scroll"] <= size["width"]+1, size
        pw.expect(dialog.get_by_role("button", name="关闭可用性详情")).to_be_in_viewport()
        pw.expect(dialog.get_by_role("button", name="完成", exact=True)).to_be_in_viewport()
        index = ["同步进度", "凭据与额度", "处理规则"].index(tab)
        ui.page.screenshot(path=str(ROOT / f".runtime/availability-v2-{width}-{'dark' if dark else 'light'}-{index}.png"))
    pw.expect(dialog).to_contain_text("自动移除异常：已关闭")
    dialog.get_by_role("tab", name="处理规则", exact=True).focus()
    ui.page.keyboard.press("Home")
    pw.expect(dialog.get_by_role("tab", name="同步进度", exact=True)).to_be_focused()
    pw.expect(dialog.get_by_role("tab", name="同步进度", exact=True)).to_have_attribute("aria-selected", "true")
    ui.page.keyboard.press("ArrowRight")
    pw.expect(dialog.get_by_role("tab", name="凭据与额度", exact=True)).to_have_attribute("aria-selected", "true")
    dialog.get_by_role("button", name="关闭可用性详情").click()
    pw.expect(dialog).not_to_be_visible()
    panel.get_by_role("button", name="详情", exact=True).click()
    dialog.get_by_role("button", name="完成", exact=True).click()
    pw.expect(dialog).not_to_be_visible()
    assert not ui.errors, ui.errors


def test_progress_unknown_nonowner_and_failure_remain_readonly(ui):
    ui.availability = inventory()
    ui.availability["maintenance_progress"].update(available=False, owner=False, active=None, totals=None)
    ui.open()
    ui.close()
    panel = ui.page.get_by_role("region", name="账号可用性", exact=True)
    pw.expect(panel.locator('[data-maintenance-active]')).to_have_text("--")
    pw.expect(panel.locator('[data-maintenance-status]').first).to_have_text("非维护实例 · 请检查网关")
    ui.maintenance_status = 503
    panel.get_by_role("button", name="详情", exact=True).click()
    pw.expect(panel.get_by_role("status")).to_contain_text("同步进度暂不可用")
    panel.get_by_role("button", name="关闭可用性详情").click()
    pw.expect(panel.get_by_role("dialog")).not_to_be_visible()
    assert not ui.posts, "read-only detail panel must not mutate/import accounts"
    assert not [error for error in ui.errors if "503" not in error], ui.errors


def test_unreachable_owner_is_not_displayed_as_stopped_or_zero(ui):
    ui.availability = inventory()
    ui.availability["maintenance_progress"].update(available=False, owner=True, instance="app0", state="unreachable",
                                                  active=None, totals=None, batch=None)
    ui.availability["maintenance"].update(mode="unavailable", batch_size=None, stale=True,
                                         reasons=["无法读取维护实例 app0 的进度；不代表后台已停止"])
    ui.open()
    ui.close()
    panel = ui.page.get_by_role("region", name="账号可用性", exact=True)
    pw.expect(panel.locator('[data-maintenance-active]')).to_have_text("--")
    pw.expect(panel.locator('[data-maintenance-status]').first).to_have_text("维护实例暂不可达")
    panel.get_by_role("button", name="详情", exact=True).click()
    pw.expect(panel).to_contain_text("尚未取得维护实例的批次信息")
    assert not ui.posts
