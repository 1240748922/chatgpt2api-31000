// Exercise the shipped bundles: this repository contains built frontend assets.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assets = path.resolve(__dirname, '../web_dist');

async function testAuthentication() {
  const source = fs.readFileSync(path.join(assets, 'assets/index-BhEm-7EJ.js'), 'utf8');
  const start = source.indexOf('const Cf=_h("auth",');
  const end = source.indexOf(';function Qv(', start);
  assert(start >= 0 && end > start);
  let now = 100000;
  let calls = 0;
  let check = async () => { throw Object.assign(new Error('gateway timeout'), {status: 504}); };
  const identity = {authenticated: true, subject: {role: 'admin', id: 'test'},
    capabilities: {admin_console: true, studio: true}, home_route: '/', version: 'test'};
  const context = {
    me: value => ({value}), N: get => ({get value() {return get();}}),
    _h: (_, setup) => setup(), La: () => ({admin_console: false, studio: false}),
    Date: {now: () => now}, Xv: 60000, xf: () => true,
    As: {login: async () => identity, logout: async () => {}, checkAuth: () => {calls++; return check();}},
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end).replace('const Cf=', 'globalThis.auth='), context);
  const auth = context.auth;
  await auth.login('test-only');
  now += 61000;
  assert.equal(await auth.checkAuth(), true);
  assert.equal(auth.capabilities.value.admin_console, true, 'transient errors must keep navigation');
  await auth.checkAuth();
  assert.equal(calls, 1, 'avoid repeated checks during retry delay');
  now += 6000;
  let finish;
  check = () => new Promise(resolve => {finish = resolve;});
  const first = auth.checkAuth(), second = auth.checkAuth();
  assert.equal(calls, 2, 'concurrent route changes share the auth request');
  finish(identity);
  await Promise.all([first, second]);
  now += 61000;
  check = async () => {throw Object.assign(new Error('expired'), {status: 401});};
  assert.equal(await auth.checkAuth(), false);
  assert.equal(auth.capabilities.value.admin_console, false, 'confirmed expiry clears permissions');
}

async function testLogPolling() {
  const source = fs.readFileSync(path.join(assets, 'replenishment-nav.js'), 'utf8');
  const start = source.lastIndexOf('  function formatLogTimestamp(value)', source.indexOf('  function formatLiveLog(status)'));
  const end = source.indexOf('\n})();', start);
  assert(start >= 0 && end > start);
  let text = 'previous log';
  let writes = 0;
  const log = {scrollHeight: 1000, clientHeight: 100, scrollTop: 100,
    get textContent() {return text;}, set textContent(value) {text = value; this.scrollTop = 0; writes++;}};
  const label = {}, dot = {};
  const page = {querySelector: selector => ({'[data-register-log]': log,
    '[data-register-log-status]': label, '[data-register-log-dot]': dot}[selector] || null)};
  let calls = 0, reply;
  let fetcher = () => new Promise(resolve => {reply = resolve;});
  const context = {
    document: {querySelector: () => page}, isRegisterRoute: () => true, authHeaders: () => ({}),
    window: {addEventListener: () => {}}, AbortSignal: {timeout: () => undefined},
    setInterval: (_, period) => assert.equal(period, 3000),
    fetch: () => {calls++; return fetcher();},
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  await context.poll();
  assert.equal(calls, 1, 'do not overlap status requests');
  reply({ok: true, json: async () => ({running: true, last_result: {reason: 'starting', stdout_tail: 'launch accepted'}})});
  await new Promise(resolve => setImmediate(resolve));
  assert(text.includes('launch accepted'), 'starting logs are visible');
  assert.equal(log.scrollTop, 100, 'preserve position while reading history');
  const oldText = text;
  fetcher = async () => {throw new Error('offline');};
  await context.poll();
  assert.equal(text, oldText, 'keep old logs on network error');
  assert.equal(writes, 1);
  assert.equal(label.textContent, 'offline');
  assert(dot.className.includes('error'));
}

function testShellTitleBeforeBodyExists() {
  const source = fs.readFileSync(path.join(assets, 'replenishment-nav.js'), 'utf8');
  const start = source.indexOf('  function syncShellTitle()');
  const end = source.indexOf('  const titleObserver', start);
  assert(start >= 0 && end > start);
  const context = {document: {body: null}};
  vm.createContext(context);
  vm.runInContext(source.slice(start, end) + ';syncShellTitle();', context);
}

async function testCleanupActionRequiresPreviewConfirmation() {
  const source = fs.readFileSync(path.join(assets, 'assets/Accounts-CQrrBRkk.js'), 'utf8');
  const start = source.indexOf('async function v(u){const payload=u===');
  const end = source.indexOf('function m(u){', start);
  assert(start >= 0 && end > start);
  let confirmed = false, calls = 0, refreshed = 0;
  const options = [];
  const context = {
    confirmAccountCleanup: async ({loadPreview, options: payload}) => {
      options.push(payload); await loadPreview(payload); return confirmed;
    },
    Q: {previewAccountCleanup: async () => ({items: []}), runAccountCleanup: async () => {calls++; return {total_removed: 2};}},
    toast: {success: () => {}},
    e: {loadData: async () => {refreshed++;}, setError: (_, error) => {throw error;}},
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  await context.v('cleanup-invalid');
  assert.equal(calls, 0, 'cancel does not delete');
  assert.equal(options[0].auto_remove_invalid_accounts, true);
  assert.equal(options[0].remove_unusable_credentials, true,
    'manual abnormal cleanup must include projected AT-invalid/no-usable-RT accounts');
  assert.equal(options[0].remove_quota_exhausted, false);
  assert.equal(options[0].auto_remove_rate_limited_accounts, false);
  confirmed = true;
  await context.v('cleanup-quota');
  assert.equal(calls, 1);
  assert.equal(refreshed, 1);
  assert.equal(options[1].remove_quota_exhausted, true);
  assert.equal(options[1].auto_remove_invalid_accounts, false);
  await context.v('cleanup-credentials');
  assert.equal(calls, 2);
  assert.equal(refreshed, 2);
  assert.equal(options[2].remove_unusable_credentials, true);
  assert.equal(options[2].auto_remove_invalid_accounts, false);
  assert(fs.readFileSync(path.join(assets, 'assets/Accounts-CQrrBRkk.js'), 'utf8')
    .includes('删除 AT/RT 失效账号'));
  assert(fs.readFileSync(path.join(assets, 'assets/Settings-CYv60EF8.js'), 'utf8')
    .includes('initialPreview:L,loadPreview:opts=>re.previewAccountCleanup(opts)'));
}

(async () => {
  await testAuthentication();
  await testLogPolling();
  testShellTitleBeforeBodyExists();
  await testCleanupActionRequiresPreviewConfirmation();
  console.log('PASS: auth, log polling, scroll retention, early page load and cleanup confirmation');
})().catch(error => {console.error(error); process.exitCode = 1;});
