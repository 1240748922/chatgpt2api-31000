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
        elif path == "/api/accounts":
            result = dict(accounts=[], total=0, all_total=0, page=1, page_size=20)
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
                events = [] if parse_qs(url.query).get("after") != ["0"] else [
                    dict(id=1, time=2, code="quota_batch", items=[
                        dict(account_label="passed@example.test", stage="quota", status="success", message="已同步"),
                        dict(account_label="failed@example.test", stage="quota", status="failed", message="auth_invalid"),
                    ])]
                result = dict(events=events, next_cursor=1)
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
            pw.expect(self.dialog).to_be_visible()
            pw.expect(self.dialog.get_by_label("选择导入任务")).to_have_value("synthetic-history")
            pw.expect(self.dialog.get_by_text("failed@example.test", exact=True)).to_be_visible()
        except AssertionError as error:
            error.add_note(f"Browser errors: {self.errors}")
            raise

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
