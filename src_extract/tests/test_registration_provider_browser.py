"""Exercise real shipped provider controls against local synthetic APIs only."""
from urllib.parse import urlsplit

import pytest

from test_replenishment_browser import ReplenishmentUI
from test_account_import_browser import browser, origin, pw


class ProviderUI(ReplenishmentUI):
    def __init__(self, page, origin):
        self.launch_sources = []
        self.settings_payloads = []
        self.provider_payloads = []
        self.reject_settings = False
        super().__init__(page, origin)
        self.config['account_replenishment'].update(registration_source='remail_target', enabled=False)

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if request.url.startswith(self.origin + '/') and request.method == 'PATCH':
            if path == '/api/settings':
                self.settings_payloads.append(request.post_data_json)
                if self.reject_settings:
                    route.fulfill(status=409, json={'detail': 'fixture settings conflict'})
                    return
                self.config['account_replenishment'] = request.post_data_json['account_replenishment']
                route.fulfill(json={'revision': 'test-saved', 'settings': self.config})
                return
            if path == '/api/accounts/replenishment/provider-config':
                self.provider_payloads.append(request.post_data_json)
                route.fulfill(json={'cfworker_has_admin_token': True,
                                    'cfworker_admin_token_masked': 'fix****ure'})
                return
        if request.url.startswith(self.origin + '/') and request.method == 'POST' and path == '/api/accounts/replenishment/run':
            self.launch_sources.append(self.config['account_replenishment']['registration_source'])
            route.fulfill(json={'reason': 'started', 'requested_count': 1})
            return
        super().route(route)


@pytest.fixture
def provider_ui(browser, origin):
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    ui = ProviderUI(context.new_page(), origin)
    ui.open_registration()
    pw.expect(ui.page.locator('[data-register-key="registration_source"]')).to_have_value('remail_target')
    pw.expect(ui.page.locator('[data-provider-save-state]')).to_contain_text('已加载')
    yield ui
    context.close()


def select_cf(ui):
    ui.page.locator('[data-register-key="registration_source"]').select_option('cfworker')
    ui.page.locator('[data-register-key="cfworker_domain"]').fill('example.invalid')


def test_save_credentials_persists_selected_source_and_domain(provider_ui):
    ui = provider_ui
    select_cf(ui)
    ui.page.locator('[data-provider-action="save"]').click()
    pw.expect(ui.page.locator('[data-provider-save-state]')).to_contain_text('已保存')
    assert ui.config['account_replenishment']['registration_source'] == 'cfworker'
    assert ui.config['account_replenishment']['cfworker_domain'] == 'example.invalid'
    assert ui.config['account_replenishment']['enabled'] is False
    assert not ui.launch_sources
    ui.page.reload()
    pw.expect(ui.page.locator('[data-register-key="registration_source"]')).to_have_value('cfworker')


def test_source_has_explicit_save_action(provider_ui):
    ui = provider_ui
    select_cf(ui)
    ui.page.locator('[data-register-source-save]').click()
    pw.expect(ui.page.locator('[data-register-source-save-state]')).to_contain_text('已保存')
    assert ui.config['account_replenishment']['registration_source'] == 'cfworker'
    assert not ui.provider_payloads and not ui.launch_sources


@pytest.mark.parametrize('action', ['manual', 'run', 'force'])
def test_unsaved_source_never_launches_old_provider(provider_ui, action):
    ui = provider_ui
    select_cf(ui)
    ui.page.locator(f'[data-register-action="{action}"]').click()
    ui.page.wait_for_timeout(300)
    assert not ui.launch_sources, 'unsaved CF selection must not start a ReMail job'


def test_provider_save_does_not_claim_success_after_settings_conflict(provider_ui):
    ui = provider_ui
    select_cf(ui)
    ui.reject_settings = True
    ui.page.locator('[data-provider-action="save"]').click()
    pw.expect(ui.page.locator('[data-provider-save-state]')).to_have_class('register-save-state error')
    assert ui.config['account_replenishment']['registration_source'] == 'remail_target'
    assert not ui.launch_sources


@pytest.mark.parametrize('save_action', ['[data-register-source-save]', '[data-provider-action="save"]',
                                         '[data-register-action="save"]'])
def test_saved_cf_source_is_used_for_next_manual_run(provider_ui, save_action):
    ui = provider_ui
    select_cf(ui)
    ui.page.locator(save_action).click()
    pw.expect(ui.page.locator('[data-register-source-save-state]')).to_contain_text('已')
    with ui.page.expect_response(lambda response: '/api/accounts/replenishment/run?' in response.url
                                 and response.request.method == 'POST') as launched:
        ui.page.locator('[data-register-action="manual"]').click()
    assert launched.value.json()['reason'] == 'started'
    assert ui.launch_sources == ['cfworker']
    assert ui.config['account_replenishment']['enabled'] is False
    assert ui.config['account_replenishment']['minimum_available'] == 100
    assert ui.config['account_replenishment']['workdir'] == '/tmp/register'
    assert not ui.errors


def test_source_save_failure_does_not_enable_old_provider_launch(provider_ui):
    ui = provider_ui
    select_cf(ui)
    ui.reject_settings = True
    ui.page.locator('[data-register-source-save]').click()
    pw.expect(ui.page.locator('[data-register-source-save-state]')).to_contain_text('fixture settings conflict')
    ui.page.locator('[data-register-action="manual"]').click()
    ui.page.wait_for_timeout(200)
    assert not ui.launch_sources
