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

(async () => {
  await testAuthentication();
  await testLogPolling();
  console.log('PASS: auth identity, shared requests, log polling, launch logs and scroll retention');
})().catch(error => {console.error(error); process.exitCode = 1;});
