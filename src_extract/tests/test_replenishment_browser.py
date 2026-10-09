"""Scroll the shipped registration page using synthetic APIs, never register accounts.

REPLENISHMENT_UI_BASELINE=a9fa2bc serves the old registration script for reproduction.
"""
import os
import subprocess
from urllib.parse import urlsplit

import pytest

from test_account_import_browser import ImportUI, ROOT, browser, origin, pw


class ReplenishmentUI(ImportUI):
    def __init__(self, page, origin):
        self.writes = []
        self.old_script = None
        self.config = dict(
            backup=dict(enabled=False, interval_minutes=60, rotation_keep=3),
            genbox_push=dict(enabled=False, timeout_secs=30),
            account_replenishment=dict(
                enabled=True, minimum_available=100, target_available=200,
                interval_seconds=30, cooldown_seconds=60, max_batch_size=100,
                workers=8, launch_timeout_seconds=600, registration_mode="password",
                registration_source="configured", workdir="/tmp/register",
                output_dir="/tmp/output", entrypoint="main.py", python_executable="python",
                extra_args="",
            ),
        )
        self.status = dict(
            running=True, current_metrics=dict(current_available=42, current_quota=456),
            last_result=dict(reason="running", stdout_tail="synthetic current registration log"),
            log_history=[dict(finished_at="2026-10-09 12:00:00", reason="replenished",
                              stdout_tail="\n".join(f"synthetic log line {i}" for i in range(300)))],
        )
        super().__init__(page, origin)

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if not request.url.startswith(self.origin + "/"):
            return super().route(route)
        baseline = os.environ.get("REPLENISHMENT_UI_BASELINE")
        if baseline and path == "/replenishment-nav.js":
            if self.old_script is None:
                self.old_script = subprocess.check_output(
                    ["git", "show", f"{baseline}:src_extract/web_dist/replenishment-nav.js"], cwd=ROOT,
                )
            route.fulfill(body=self.old_script, content_type="text/javascript")
            return
        if request.method not in {"GET", "HEAD"}:
            self.writes.append((request.method, path))
            route.fulfill(status=400, json={"detail": "read-only UI test"})
            return
        if path == "/api/settings":
            route.fulfill(json=dict(revision="test-only", settings=self.config, fields={}))
        elif path == "/api/accounts/replenishment":
            route.fulfill(json=self.status)
        elif path == "/api/accounts/replenishment/provider-config":
            route.fulfill(json=dict(registration_proxy="", account_proxy=""))
        else:
            super().route(route)

    def open_registration(self):
        self.page.goto(self.origin + "/#/settings?tab=replenishment")
        pw.expect(self.page.locator("[data-account-replenishment-page]")).to_have_count(1)
        pw.expect(self.page.locator('[data-register-key="workdir"]')).to_have_value("/tmp/register")
        pw.expect(self.page.locator('[data-provider-action="save"]')).to_have_count(1)
        pw.expect(self.page.locator('[data-register-log]')).to_contain_text("synthetic log line 299")
        assert not self.errors, self.errors


@pytest.fixture
def registration_ui(browser, origin):
    context = browser.new_context(viewport=dict(width=1440, height=900))
    ui = ReplenishmentUI(context.new_page(), origin)
    yield ui
    context.close()


def wheel_over(page, locator, delta):
    box = locator.bounding_box()
    viewport = page.viewport_size
    page.mouse.move(min(box["x"] + box["width"] / 2, viewport["width"] - 10),
                    min(box["y"] + min(40, box["height"] / 2), viewport["height"] - 10))
    page.mouse.wheel(0, delta)


def assert_inside_viewport(locator):
    bounds = locator.evaluate("""el => {
        const r = el.getBoundingClientRect();
        const clipped = [];
        for (let p = el.parentElement; p; p = p.parentElement) {
            const css = getComputedStyle(p), pr = p.getBoundingClientRect();
            if (['hidden', 'clip', 'auto', 'scroll'].includes(css.overflowY))
                clipped.push({top: pr.top, bottom: pr.bottom});
        }
        return {top:r.top, bottom:r.bottom, left:r.left, right:r.right,
                width:innerWidth, height:innerHeight, clipped};
    }""")
    assert 0 <= bounds["top"] < bounds["bottom"] <= bounds["height"], bounds
    assert 0 <= bounds["left"] < bounds["right"] <= bounds["width"], bounds
    for clip in bounds["clipped"]:
        assert clip["top"] - 1 <= bounds["top"] and bounds["bottom"] <= clip["bottom"] + 1, bounds


@pytest.mark.parametrize("viewport", [(1920, 1080), (1440, 900), (1280, 720), (1024, 768)])
def test_desktop_bottom_settings_and_logs_reachable_by_wheel(registration_ui, viewport):
    ui = registration_ui
    ui.page.set_viewport_size(dict(width=viewport[0], height=viewport[1]))
    ui.open_registration()
    stack = ui.page.locator(".register-stack")
    wheel_over(ui.page, stack, 10000)
    ui.page.wait_for_function("document.querySelector('.register-stack').scrollTop > 0", timeout=3000)
    bottom_save = ui.page.locator('[data-register-action="save"]')
    assert_inside_viewport(bottom_save)
    before = stack.evaluate("el => el.scrollTop")
    log = ui.page.locator("[data-register-log]")
    log.evaluate("el => el.scrollTop = 0")
    wheel_over(ui.page, log, 1000)
    ui.page.wait_for_function("document.querySelector('[data-register-log]').scrollTop > 0", timeout=3000)
    assert stack.evaluate("el => el.scrollTop") == before
    assert not ui.writes and not ui.errors, (ui.writes, ui.errors)
    ui.page.screenshot(path=str(ROOT / f".runtime/register-scroll-fixed-{viewport[0]}x{viewport[1]}.png"))


@pytest.mark.parametrize("viewport", [(1000, 720), (800, 600), (390, 640), (1100, 480), (1280, 600)])
def test_narrow_screen_scrolls_to_bottom_and_back(registration_ui, viewport):
    ui = registration_ui
    ui.page.set_viewport_size(dict(width=viewport[0], height=viewport[1]))
    ui.open_registration()
    wheel_over(ui.page, ui.page.locator(".register-stack"), 10000)
    ui.page.wait_for_timeout(300)
    # At this width the whole page scrolls; the log below the form is reachable.
    log = ui.page.locator("[data-register-log]")
    assert_inside_viewport(log)
    ui.page.mouse.move(viewport[0] - 5, viewport[1] / 2)
    ui.page.mouse.wheel(0, -10000)
    ui.page.wait_for_timeout(300)
    assert_inside_viewport(ui.page.get_by_role("button", name="刷新状态", exact=True))
    assert not ui.writes and not ui.errors, (ui.writes, ui.errors)
