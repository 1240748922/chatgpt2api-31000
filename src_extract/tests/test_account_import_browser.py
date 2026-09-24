"""Exercise the shipped Vue app in Chromium, not mocked render helpers.

python -m pytest src_extract/tests/test_account_import_browser.py -q
Requires playwright and its Chromium browser (`python -m playwright install chromium`).
All HTTP APIs are intercepted with synthetic data. No account DB or upstream is used.
IMPORT_UI_BASELINE=ffa402a serves that revision's assets to reproduce the regression.
"""
import json
import os
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import pytest

pw = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "src_extract" / "web_dist"


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture(scope="module")
def origin():
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(WEB)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


class ImportUI:
    def __init__(self, page, origin):
        self.page, self.origin = page, origin
        self.errors, self.posts, self.pending = [], [], []
        self.hold_posts = False
        self.extra_items = 0
        self.extra_events = 0
        self.dashboard = None
        self.accounts = []
        self.availability = None
        self.availability_status = 200
        self.availability_calls = 0
        self.maintenance_calls = 0
        self.maintenance_status = 200
        self.jobs = [dict(id="synthetic-history", status="completed", total=2,
                         processed=2, saved=2, added=2, skipped=0, synced=1,
                         sync_failed=1, done=True, created_at=1, updated_at=2)]
        self.baseline = os.environ.get("IMPORT_UI_BASELINE", "")
        self.assets = {}
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.on("console", lambda message: self.errors.append(message.text) if message.type == "error" else None)
        page.add_init_script("localStorage.setItem('chatgpt2api.adminKey', 'synthetic-test-only')")
        page.route("**/*", self.route)

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path
        if not request.url.startswith(self.origin + "/"):
            # The shell optionally loads a Google font. Use system fonts in
            # this offline test; never send test credentials off localhost.
            if url.netloc != "fonts.googleapis.com" or request.resource_type != "stylesheet":
                self.errors.append(f"Unexpected external network request: {url.netloc}")
            route.fulfill(body="", content_type="text/css")
            return
        if self.baseline and (path.startswith("/assets/") or path == "/"):
            key = path.lstrip("/") or "index.html"
            if key not in self.assets:
                self.assets[key] = subprocess.check_output(["git", "show", f"{self.baseline}:src_extract/web_dist/{key}"], cwd=ROOT)
            mime = "text/javascript" if key.endswith(".js") else "text/css" if key.endswith(".css") else "text/html"
            route.fulfill(body=self.assets[key], content_type=mime)
            return
        if not path.startswith(("/api/", "/auth/", "/version")):
            route.continue_()
            return
        result = {}
        if path.startswith("/auth/"):
            result = dict(authenticated=True, version="ui-test", subject=dict(id="test", name="测试管理员", role="admin"),
                          capabilities=dict(admin_console=True, studio=True), home_route="/")
        elif path == "/version":
            result = dict(version=(ROOT / "src_extract/VERSION").read_text().strip())
        elif path == "/api/dashboard":
            result = self.dashboard
        elif path == "/api/accounts":
            result = dict(items=self.accounts, total=len(self.accounts), all_total=len(self.accounts), page=1, page_size=20)
        elif path == "/api/accounts/availability":
            self.availability_calls += 1
            route.fulfill(json=self.availability or {}, status=self.availability_status)
            return
        elif path == "/api/accounts/maintenance-status":
            self.maintenance_calls += 1
            result = {key: value for key, value in (self.availability or {}).items()
                      if key in {"maintenance", "maintenance_progress", "cleanup_policy"}}
            route.fulfill(json=result, status=self.maintenance_status)
            return
        elif path == "/api/account-groups":
            result = dict(groups=[dict(id="test-group", name="测试分组", enabled=True, account_count=0)], revision="test")
        elif path == "/api/account-import-jobs":
            if request.method == "POST":
                payload = request.post_data_json
                self.posts.append(payload)
                job = dict(self.jobs[0], id=f"synthetic-job-{len(self.posts)}", total=len(payload["accounts"]),
                           status="saving", done=False, saved=0, processed=0)
                self.jobs.insert(0, job)
                if self.hold_posts:
                    self.pending.append((route, job))
                    return
                result = dict(job=job)
            else:
                result = dict(jobs=self.jobs)
        elif path.startswith("/api/account-import-jobs/"):
            job_id = path.split("/")[3]
            if path.endswith("/events"):
                all_events = [
                    dict(id=1, time=2, code="quota_batch", start=1, end=2, synced=1, failed=1, items=[
                        dict(account_label="passed@example.test", stage="quota", status="success", message="已同步"),
                        dict(account_label="failed@example.test", stage="quota", status="failed", message="auth_invalid"),
                    ] + [dict(account_label=f"extra-{i}@example.test", stage="refresh", status="success", message="RT 已兑换")
                         for i in range(self.extra_items)])]
                all_events.extend(dict(id=i+2, time=i+3, code="batch_saved", start=i+1, end=i+1,
                                       saved=1, added=1, duration_ms=10) for i in range(self.extra_events))
                after = int(parse_qs(url.query).get("after", ["0"])[0])
                events = [event for event in all_events if event["id"] > after][:100]
                result = dict(events=events, next_cursor=events[-1]["id"] if events else after)
            else:
                result = dict(job=next(job for job in self.jobs if job["id"] == job_id))
        route.fulfill(json=result)

    @property
    def dialog(self):
        return self.page.get_by_role("dialog", name="导入账号", exact=True)

    @property
    def textarea(self):
        return self.dialog.locator("textarea")

    def open(self, mode="refresh_token"):
        self.page.goto(self.origin + f"/#/accounts?import={mode}")
        try:
            # git-show interception spawns a process per old asset on Windows.
            pw.expect(self.dialog).to_be_visible(timeout=30000 if self.baseline else 5000)
            self.show_logs()
            pw.expect(self.dialog.get_by_label("选择导入任务")).to_have_value("synthetic-history")
            pw.expect(self.dialog.get_by_text("failed@example.test", exact=True)).to_be_visible()
            self.show_input()
        except AssertionError as error:
            error.add_note(f"Browser errors: {self.errors}")
            raise

    def show_logs(self):
        if self.baseline and not self.dialog.get_by_role("tab", name="任务日志", exact=True).count():
            return  # Old revisions stacked the form and logs instead of tabs.
        self.dialog.get_by_role("tab", name="任务日志", exact=True).click()

    def show_input(self):
        if self.baseline and not self.dialog.get_by_role("tab", name="导入账号", exact=True).count():
            return
        self.dialog.get_by_role("tab", name="导入账号", exact=True).click()

    def upload(self, records, name="accounts.json"):
        body = records if isinstance(records, str) else json.dumps(records)
        self.dialog.get_by_label("选择账号文件").set_input_files(dict(name=name, mimeType="application/json", buffer=body.encode()))

    def close(self):
        self.dialog.get_by_role("button", name="关闭", exact=True).click()
        pw.expect(self.dialog).to_have_count(0)
        assert not self.errors, self.errors

    def reopen(self):
        self.page.get_by_role("button", name="导入 / 添加", exact=True).click()
        self.page.get_by_role("menuitem", name="导入 Refresh Token", exact=True).click()
        pw.expect(self.dialog).to_have_count(1)


@pytest.fixture
def ui(browser, origin):
    context = browser.new_context(viewport=dict(width=1440, height=1050))
    instance = ImportUI(context.new_page(), origin)
    yield instance
    context.close()


def test_history_upload_submit_close_and_reopen(ui):
    ui.open()
    # Populated history triggers the createBaseVNode regression before a file
    # is selected. An empty history alone used to hide it from unit tests.
    assert not ui.errors, ui.errors
    ui.upload([dict(email="synthetic@example.test", access_token="synthetic-at", refresh_token="opaque-rt")])
    pw.expect(ui.textarea).to_have_value("opaque-rt")
    start = ui.dialog.get_by_role("button", name="开始导入", exact=True)
    pw.expect(start).to_be_enabled()
    pw.expect(start.locator("svg")).to_have_count(1)
    assert "ui-btn-primary" in start.get_attribute("class").split()
    assert "," not in start.get_attribute("class")
    ui.page.screenshot(path=str(ROOT / ".runtime/import-ui-fixed.png")) if (ROOT / ".runtime").exists() else None
    start.click()
    pw.expect(ui.dialog.get_by_label("选择导入任务")).to_have_value("synthetic-job-1")
    assert ui.posts[0]["accounts"] == [dict(refresh_token="opaque-rt", source_type="web")]
    pw.expect(ui.textarea).to_have_value("")
    ui.close()
    for _ in range(3):
        ui.reopen()
        pw.expect(ui.dialog.get_by_label("选择导入任务")).to_have_value("synthetic-job-1")
        ui.close()


def test_large_log_scroll_is_bounded(ui):
    ui.extra_items = 1000
    ui.extra_events = 400
    ui.open()
    ui.show_logs()
    table_scroll = ui.dialog.locator('table').locator('..')
    sizes = table_scroll.evaluate('(el) => ({client:el.clientHeight, scroll:el.scrollHeight, height:el.getBoundingClientRect().height})')
    assert sizes['scroll'] > sizes['client']
    assert sizes['height'] < 500, sizes
    assert ui.dialog.locator('tbody tr').count() == 100
    bounds = ui.dialog.evaluate('(el) => ({client:el.clientHeight, scroll:el.scrollHeight})')
    assert bounds['scroll'] <= bounds['client'] + 1, bounds
    ui.dialog.get_by_role('button', name='下一页', exact=True).click()
    pw.expect(ui.dialog.get_by_label('明细分页')).to_have_text('第 2 / 11 页 · 1002 条记录')
    ui.page.screenshot(path=str(ROOT / '.runtime/import-tabs-logs.png'))
    ui.dialog.get_by_role('tab', name='原始日志', exact=True).click()
    raw = ui.dialog.get_by_role('tabpanel', name='原始日志', exact=True)
    pw.expect(raw).to_be_visible()
    pw.expect(raw).to_contain_text('第 400–400 条')
    assert raw.evaluate('el => el.scrollHeight > el.clientHeight')
    assert not ui.dialog.locator('details').count()
    ui.close()


def test_many_import_jobs_do_not_grow_modal_or_history_list(ui):
    ui.jobs.extend(dict(ui.jobs[0], id=f"history-{i}") for i in range(1000))
    ui.extra_items = 1000
    ui.extra_events = 400
    ui.open()
    ui.show_logs()
    options = ui.dialog.get_by_label("选择导入任务").locator("option")
    pw.expect(options).to_have_count(30)
    pw.expect(ui.dialog.get_by_text("仅展示最近 30 个任务", exact=False)).to_be_visible()
    ui.dialog.get_by_label("选择导入任务").select_option("history-28")
    pw.expect(ui.dialog.get_by_text("failed@example.test", exact=True)).to_be_visible()
    bounds = ui.dialog.evaluate("el => ({client:el.clientHeight, scroll:el.scrollHeight})")
    assert bounds["scroll"] <= bounds["client"] + 1, bounds
    ui.page.screenshot(path=str(ROOT / ".runtime/import-history-bounded.png"))
    ui.close()


@pytest.mark.parametrize("mode", ["complete", "partial", "legacy", "single"])
def test_dashboard_current_concurrency_uses_cluster_sample(ui, mode):
    from dashboard_fixtures import dashboard_payload
    ui.dashboard = dashboard_payload()
    operations = ui.dashboard["operations"]
    if mode == "partial":
        operations.update(active_requests=125, responding_instances=6, complete=False)
        ui.dashboard["metrics"].update(status="degraded", ready=False, stale=True)
    elif mode == "legacy":
        ui.dashboard["operations"] = dict(active_requests=1)
    elif mode == "single":
        operations.update(active_requests=0, scope="instance", expected_instances=1, responding_instances=1)
    ui.page.goto(ui.origin + "/#/")
    label = ui.page.get_by_text("当前并发", exact=True)
    pw.expect(label).to_be_visible()
    card = label.locator("..")
    if mode == "complete":
        pw.expect(card).to_contain_text("199")
        pw.expect(card).to_contain_text("集群采样 · 8/8 实例")
        ui.page.screenshot(path=str(ROOT / ".runtime/dashboard-cluster-count.png"))
    elif mode == "partial":
        pw.expect(card).to_contain_text("≥ 125")
        pw.expect(card).to_contain_text("6/8 实例")
        pw.expect(ui.page.get_by_text("并发统计不完整", exact=False)).to_be_visible()
        pw.expect(ui.page.get_by_text("统计数据暂未更新", exact=False)).to_be_visible()
    elif mode == "legacy":
        pw.expect(card).to_contain_text("仅当前实例（旧版统计）")
    else:
        pw.expect(card).to_contain_text("0")
        pw.expect(card).to_contain_text("当前实例采样")
    # No chart/total changes: counters and warnings must still refresh, rather
    # than be skipped by the dashboard's unchanged-metrics fingerprint.
    ui.dashboard = dashboard_payload()
    ui.dashboard["operations"]["active_requests"] = 0
    ui.page.get_by_role("button", name="刷新当前页面", exact=True).click()
    pw.expect(card).to_contain_text("集群采样 · 8/8 实例")
    pw.expect(card.locator("p").nth(1)).to_have_text("0")
    pw.expect(ui.page.get_by_text("并发统计不完整", exact=False)).to_have_count(0)
    assert not ui.errors, ui.errors


def test_minimize_keeps_draft_and_releases_page(ui):
    ui.open()
    ui.textarea.fill('synthetic-draft-rt')
    ui.dialog.get_by_text('入库后在后台同步账号信息与额度', exact=True).click()
    for _ in range(3):
        ui.dialog.get_by_role('button', name='最小化导入窗口').click()
        pw.expect(ui.dialog).to_have_count(0)
        dock = ui.page.get_by_role('dialog', name='已最小化的导入任务', exact=True)
        pw.expect(dock).to_be_visible()
        assert dock.get_attribute('aria-modal') is None
        assert ui.page.locator('body').evaluate('el => el.style.overflow') != 'hidden'
        ui.page.get_by_role('button', name='刷新列表', exact=True).click()
        assert dock.bounding_box()['height'] < 100
        ui.page.screenshot(path=str(ROOT / '.runtime/import-minimized.png'))
        ui.page.get_by_role('button', name='恢复导入窗口').click()
        pw.expect(ui.dialog).to_have_count(1)
        pw.expect(ui.textarea).to_have_value('synthetic-draft-rt')
        pw.expect(ui.dialog.get_by_role('checkbox', name='入库后在后台同步账号信息与额度')).not_to_be_checked()
    assert not ui.posts
    ui.close()


def test_minimize_job_still_polls_and_menu_restores_same_panel(ui):
    ui.open()
    ui.textarea.fill('synthetic-rt')
    ui.dialog.get_by_role('button', name='开始导入', exact=True).click()
    pw.expect(ui.dialog.get_by_role('tab', name='任务日志', exact=True)).to_have_attribute('aria-selected', 'true')
    ui.dialog.get_by_label('筛选导入结果').select_option('failed')
    ui.dialog.get_by_role('button', name='最小化导入窗口').click()
    ui.jobs[0].update(status='syncing', processed=1, saved=1)
    dock = ui.page.get_by_role('dialog', name='已最小化的导入任务', exact=True)
    pw.expect(dock).to_contain_text('后台同步额度中 · 1/1')
    ui.reopen()
    pw.expect(dock).to_have_count(0)
    pw.expect(ui.dialog.get_by_role('tab', name='任务日志', exact=True)).to_have_attribute('aria-selected', 'true')
    pw.expect(ui.dialog.get_by_label('筛选导入结果')).to_have_value('failed')
    assert len(ui.posts) == 1
    ui.close()


def test_minimize_during_submit_then_close_dock(ui):
    ui.open()
    ui.hold_posts = True
    ui.textarea.fill('synthetic-rt')
    ui.dialog.get_by_role('button', name='开始导入', exact=True).click()
    pw.expect(ui.dialog.get_by_role('button', name='正在提交…', exact=True)).to_be_disabled()
    ui.dialog.get_by_role('button', name='最小化导入窗口').click()
    route, job = ui.pending.pop()
    route.fulfill(json=dict(job=job))
    dock = ui.page.get_by_role('dialog', name='已最小化的导入任务', exact=True)
    pw.expect(dock).to_contain_text('分批入库中')
    ui.page.get_by_role('button', name='恢复导入窗口').click()
    pw.expect(ui.dialog.get_by_label('选择导入任务')).to_have_value('synthetic-job-1')
    ui.dialog.get_by_role('button', name='最小化导入窗口').click()
    dock.get_by_role('button', name='关闭导入窗口').click()
    pw.expect(dock).to_have_count(0)
    ui.reopen()
    pw.expect(ui.dialog.get_by_label('选择导入任务')).to_have_value('synthetic-job-1')
    assert len(ui.posts) == 1
    ui.close()


def test_minimize_during_file_read_keeps_completed_input(ui):
    ui.open()
    ui.page.evaluate("""() => {
        File.prototype.text = function() {
            return new Promise(resolve => { window.finishFileRead = resolve; });
        };
    }""")
    ui.upload(dict(refresh_token='file-rt'))
    pw.expect(ui.dialog.get_by_role('button', name='读取文件中…', exact=True)).to_be_disabled()
    ui.dialog.get_by_role('button', name='最小化导入窗口').click()
    ui.page.evaluate("window.finishFileRead('{\"refresh_token\":\"file-rt\"}')")
    ui.page.get_by_role('button', name='恢复导入窗口').click()
    pw.expect(ui.textarea).to_have_value('file-rt')
    pw.expect(ui.dialog.get_by_role('button', name='开始导入', exact=True)).to_be_enabled()
    assert not ui.posts
    ui.close()


def test_minimized_import_survives_navigation(ui):
    ui.open()
    ui.textarea.fill('draft-across-navigation')
    ui.dialog.get_by_role('button', name='最小化导入窗口').click()
    ui.page.get_by_role('link', name='日志管理', exact=True).click()
    pw.expect(ui.page).to_have_url(ui.origin + '/#/logs')
    assert ui.page.locator('body').evaluate('el => el.style.overflow') != 'hidden'
    ui.page.get_by_role('link', name='账号管理', exact=True).click()
    ui.page.get_by_role('button', name='恢复导入窗口').click()
    pw.expect(ui.textarea).to_have_value('draft-across-navigation')
    assert not ui.posts
    ui.close()


def test_nonlocal_import_form_keeps_original_controls(ui):
    ui.open()
    ui.dialog.get_by_role('button', name='OAuth 登录已有账号', exact=True).click()
    pw.expect(ui.dialog.get_by_placeholder('name@example.com')).to_be_visible()
    ui.dialog.get_by_role('button', name='最小化导入窗口').click()
    ui.page.get_by_role('button', name='恢复导入窗口').click()
    pw.expect(ui.dialog.get_by_placeholder('name@example.com')).to_be_visible()
    ui.close()


@pytest.mark.parametrize('size', [(1366, 768), (800, 600), (390, 844)])
def test_import_window_fits_viewport_with_large_history(ui, size):
    ui.page.set_viewport_size(dict(width=size[0], height=size[1]))
    ui.extra_items = 500
    ui.open()
    ui.show_logs()
    bounds = ui.dialog.bounding_box()
    assert bounds['y'] >= 0 and bounds['y'] + bounds['height'] <= size[1], bounds
    assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= size[0], bounds
    assert ui.dialog.evaluate('el => el.scrollHeight <= el.clientHeight + 1')
    scroll = ui.dialog.get_by_label('账号明细滚动区域')
    assert scroll.bounding_box()['height'] > 60
    assert scroll.evaluate('el => el.scrollHeight > el.clientHeight')
    ui.close()


@pytest.mark.parametrize("mode,key", [("access_token", "accessToken"), ("refresh_token", "refreshToken")])
def test_500_nested_json_accounts_plain_lines_and_submit(ui, mode, key):
    ui.open(mode)
    values = [f"synthetic-{mode}-{i}" for i in range(500)]
    records = dict(data=[dict(email=f"synthetic-{i}@example.test", tokens={key: token}) for i, token in enumerate(values)])
    ui.upload(records)
    pw.expect(ui.textarea).to_have_value("\n".join(values))
    ui.dialog.get_by_role("button", name="开始导入", exact=True).click()
    pw.expect(ui.dialog.get_by_label("选择导入任务")).to_have_value("synthetic-job-1")
    assert [account[mode] for account in ui.posts[0]["accounts"]] == values
    ui.close()


def test_typing_file_append_and_file_picker(ui):
    ui.open("access_token")
    ui.textarea.fill("synthetic-pasted-at")
    with ui.page.expect_file_chooser() as chooser:
        ui.dialog.get_by_role("button", name="读取 TXT / JSON 文件", exact=True).click()
    chooser.value.set_files(dict(name="file.json", mimeType="application/json", buffer=b'{"access_token":"synthetic-file-at"}'))
    pw.expect(ui.textarea).to_have_value("synthetic-pasted-at\nsynthetic-file-at")
    # Same file can be selected again after the native input was cleared.
    ui.upload(dict(access_token="synthetic-file-at"), name="file.json")
    pw.expect(ui.textarea).to_have_value("synthetic-pasted-at\nsynthetic-file-at\nsynthetic-file-at")
    ui.close()


@pytest.mark.parametrize("payload", ["not-json", "{broken", '{"access_token":"wrong-mode"}'])
def test_invalid_file_does_not_enable_submit_or_lock_close(ui, payload):
    ui.open()
    ui.upload(payload)
    pw.expect(ui.dialog.get_by_role("status").filter(has_text="第 1 个文件")).to_be_visible()
    pw.expect(ui.textarea).to_have_value("")
    pw.expect(ui.dialog.get_by_role("button", name="开始导入", exact=True)).to_be_disabled()
    assert not ui.posts
    ui.close()


def test_close_during_submission_and_resume_accepted_job(ui):
    ui.open()
    ui.hold_posts = True
    ui.textarea.fill("synthetic-rt")
    ui.dialog.get_by_role("button", name="开始导入", exact=True).click()
    pw.expect(ui.dialog.get_by_role("button", name="正在提交…", exact=True)).to_be_disabled()
    assert len(ui.pending) == 1
    ui.close()
    route, job = ui.pending.pop()
    route.fulfill(json=dict(job=job))
    ui.page.wait_for_function("localStorage.getItem('chatgpt2api.importJob') === 'synthetic-job-1'")
    ui.reopen()
    pw.expect(ui.dialog.get_by_label("选择导入任务")).to_have_value("synthetic-job-1")
    ui.textarea.fill("synthetic-next-rt")
    pw.expect(ui.dialog.get_by_role("button", name="开始导入", exact=True)).to_be_enabled()
    ui.close()


def test_close_during_file_read_does_not_change_reopened_panel(ui):
    ui.open()
    ui.page.evaluate("""() => {
        window.originalFileText = File.prototype.text;
        File.prototype.text = function() {
            return new Promise(resolve => { window.finishFileRead = resolve; });
        };
    }""")
    ui.upload(dict(refresh_token="old-panel-rt"))
    pw.expect(ui.dialog.get_by_role("button", name="读取文件中…", exact=True)).to_be_disabled()
    ui.close()
    ui.reopen()
    ui.textarea.fill("new-panel-rt")
    ui.page.evaluate("""() => {
        File.prototype.text = window.originalFileText;
        window.finishFileRead('{"refresh_token":"old-panel-rt"}');
    }""")
    pw.expect(ui.textarea).to_have_value("new-panel-rt")
    pw.expect(ui.dialog.get_by_role("button", name="开始导入", exact=True)).to_be_enabled()
    assert not ui.posts
    ui.close()


def test_target_group_and_quota_sync_option_are_preserved(ui):
    ui.open()
    ui.dialog.get_by_role("button", name="目标分组", exact=True).click()
    ui.page.get_by_role("option", name="测试分组", exact=True).click()
    # The original checkbox visually replaces its sr-only input; click the
    # visible label just as the user does, then assert the underlying state.
    ui.dialog.get_by_text("入库后在后台同步账号信息与额度", exact=True).click()
    pw.expect(ui.dialog.get_by_role("checkbox", name="入库后在后台同步账号信息与额度")).not_to_be_checked()
    ui.upload(dict(refreshToken="synthetic-rt"))
    ui.dialog.get_by_role("button", name="开始导入", exact=True).click()
    pw.expect(ui.dialog.get_by_label("选择导入任务")).to_have_value("synthetic-job-1")
    assert ui.posts[0]["target_group_id"] == "test-group"
    assert ui.posts[0]["sync_after_import"] is False
    ui.close()


def test_mode_switch_logs_filter_and_close(ui):
    ui.open()
    ui.show_logs()
    ui.dialog.get_by_label("筛选导入结果").select_option("failed")
    pw.expect(ui.dialog.get_by_text("passed@example.test", exact=True)).to_have_count(0)
    pw.expect(ui.dialog.get_by_text("failed@example.test", exact=True)).to_be_visible()
    ui.dialog.get_by_role("button", name="导入 Access Token", exact=True).click()
    pw.expect(ui.dialog.get_by_placeholder("一行一个 access token，或粘贴账号 JSON")).to_be_visible()
    ui.upload(dict(accessToken="synthetic-at", refreshToken="synthetic-rt"))
    pw.expect(ui.textarea).to_have_value("synthetic-at")
    # This original modal deliberately disables backdrop/Escape dismissal.
    # Its header close control must work after replacing the import panel.
    ui.close()


@pytest.mark.parametrize("mode", ["session_json", "cpa_json", "sub2api_json"])
def test_structured_import_still_submits_credentials(ui, mode):
    ui.open(mode)
    ui.upload([dict(access_token="synthetic-at", refresh_token="synthetic-rt", group_id="test-group", proxy="direct")])
    pw.expect(ui.dialog.get_by_role("button", name="开始导入", exact=True)).to_be_enabled()
    ui.dialog.get_by_role("button", name="开始导入", exact=True).click()
    pw.expect(ui.dialog.get_by_label("选择导入任务")).to_have_value("synthetic-job-1")
    account = ui.posts[0]["accounts"][0]
    assert account["access_token"] == "synthetic-at" and account["refresh_token"] == "synthetic-rt"
    if mode != "session_json":
        assert account["source_type"] == "codex"
        assert account["group_id"] == "test-group" and account["proxy"] == "direct"
    ui.close()
