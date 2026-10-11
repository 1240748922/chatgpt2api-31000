// Run the shipped edit-modal/controller code with synthetic APIs, never a live proxy.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');
const assets = path.resolve(__dirname, '../web_dist/assets');
const baseline = process.env.ACCOUNTS_PROXY_UI_BASELINE;
const source = baseline
  ? execFileSync('git', ['show', `${baseline}:src_extract/web_dist/assets/Accounts-CQrrBRkk.js`],
      {cwd:path.resolve(__dirname, '../..'), encoding:'utf8'})
  : fs.readFileSync(path.join(assets, 'Accounts-CQrrBRkk.js'), 'utf8');
const proxySource = fs.readFileSync(path.join(assets, 'proxyView-BbuuzHJf.js'), 'utf8');
const complete = 'http://synthetic-region-US-sid-NEW01234-t-5:p%40ss%3Aword@proxy.example.test:8080';

function slice(text, start, end) {
  const first = text.indexOf(start), last = text.indexOf(end, first);
  assert(first >= 0 && last > first, `missing shipped code boundary: ${start}`);
  return text.slice(first, last);
}

function fixture() {
  const tested = [], saved = [], notices = [];
  let detail = {id:'synthetic-account', email:'user@example.test', quota_unknown:false,
    proxy:'custom', proxy_mode:'custom', proxy_label:'http://proxy.example.test:8080',
    configuration:{type:'free', source_type:'web', group_id:'', quota:8, proxy:complete}};
  const context = {
    T:value => ({value}), R:get => ({get value() {return get();}}), os:value => value,
    De:() => Object.fromEntries(['info', 'warning', 'success', 'error'].map(key =>
      [key, message => notices.push({key, message})])),
    Ke:() => ({ask:async() => true}),
    jo:{test:async url => {tested.push(url);return {result:{ok:true, latency_ms:1, status:200}};},
      testGroup:async args => {tested.push(args.id);return {summary:{tone:'success', message:'ok'}};}},
    Ls:tone => tone,
    Q:{get:async() => detail, upsert:async payload => {saved.push(payload);
      return {errors:[], removed_ids:[], account:detail};}},
  };
  vm.createContext(context);
  vm.runInContext(slice(proxySource, 'function R(e,t=', 'const V=').replace('function R(', 'function _e(')
    + slice(source, 'const sn=', 'function ln(e)')
    + slice(source, 'function qo()', 'function Fl(e)'), context);
  const modal = context.Ll({isBatchBusy:() => false, loadAccountGroups:async() => {},
    loadData:async() => {}, setError:(_label, error) => {throw error;}});
  const value = {get value() {return modal.form.proxy;}, set value(proxy) {modal.form.proxy=proxy;}};
  const controls = context.an({proxyValue:value, proxyGroups:{value:[{id:'test-group', name:'Test'}]},
    setError:(_label, error) => {throw error;}});
  modal.setProxyControlsSync(controls.syncProxyControlsFromProjection);
  return {modal, controls, value, tested, saved, notices, setDetail:next => {detail=next;}};
}

async function testActualEditTestAndSave() {
  const f = fixture();
  await f.modal.openEditModal({id:'synthetic-account'});
  assert.equal(f.modal.showModal.value, true);
  assert.equal(f.controls.customProxyInput.value, complete,
    'edit input must use configuration.proxy, not list placeholder custom');
  assert.equal(f.value.value, complete);
  assert.equal(f.controls.accountProxyPreview.value, 'http://proxy.example.test:8080',
    'summary keeps its redacted projection');
  await f.controls.testAccountProxy();
  assert.deepEqual(f.tested, [complete]);
  await f.modal.saveAccount();
  assert.equal(f.saved.length, 1);
  assert.equal(f.saved[0].proxy, complete);
  assert.equal(f.value.value, '');
  assert.equal(f.controls.customProxyInput.value, '');
  assert.equal(f.modal.showModal.value, false);
}

async function testModeToggleDoesNotWritePlaceholderBack() {
  const f = fixture();
  await f.modal.openEditModal({id:'synthetic-account'});
  f.controls.setProxyMode('direct');
  assert.equal(f.value.value, 'direct');
  f.controls.setProxyMode('custom');
  assert.equal(f.value.value, complete, 'restoring custom keeps authentication and sticky SID');
  await f.modal.saveAccount();
  assert.equal(f.saved[0].proxy, complete);
}

function testFormFallbackAndMissingFullValue() {
  const f = fixture();
  f.value.value = complete;
  f.controls.syncProxyControlsFromProjection({proxy_mode:'custom', proxy:'custom'});
  assert.equal(f.controls.customProxyInput.value, complete, 'pre-filled raw form is authoritative');
  f.value.value = '';
  f.controls.syncProxyControlsFromProjection({proxy_mode:'custom', proxy:'custom'});
  assert.equal(f.controls.customProxyInput.value, '', 'placeholder is never a usable URL');
  f.controls.syncProxyControlsFromProjection({proxy_mode:'custom', proxy:complete});
  assert.equal(f.controls.customProxyInput.value, complete, 'legacy full projection still works');
}

function testExplicitEmptyDetailsAndOtherModes() {
  const f = fixture();
  for (const [mode, proxy, shownMode] of [
    ['inherit', '', 'global'], ['direct', 'direct', 'direct'],
    ['group', 'group:test-group', 'group'], ['profile', 'profile:legacy', 'custom'],
  ]) {
    f.value.value = complete; // A previous edit must not leak into the next account.
    f.controls.syncProxyControlsFromProjection({proxy_mode:mode, proxy,
      proxy_group_id:mode==='group'?'test-group':'', configuration:{proxy}});
    assert.equal(f.value.value, proxy);
    assert.equal(f.controls.proxyMode.value, shownMode);
    assert.equal(f.controls.customProxyInput.value, mode==='profile'?proxy:'');
  }
  f.controls.selectProxyGroup('test-group');
  assert.equal(f.value.value, 'group:test-group');
  f.controls.setProxyMode('global');
  assert.equal(f.value.value, '');
}

function testAssetCacheBustTargetsOnlyEditedChunks() {
  const version = '20261010-account-proxy-v1';
  for (const name of ['AppShell-BUNwzyHY.js', 'index-BhEm-7EJ.js']) {
    const text = fs.readFileSync(path.join(assets, name), 'utf8');
    assert(text.includes(`Accounts-CQrrBRkk.js?v=${version}`));
  }
  const index = fs.readFileSync(path.join(assets, 'index-BhEm-7EJ.js'), 'utf8');
  assert(index.includes(`AppShell-BUNwzyHY.js?v=${version}`));
}

async function testEditedUrlAndCancelIsolation() {
  const f = fixture();
  await f.modal.openEditModal({id:'synthetic-account'});
  const replacement = 'socks5h://synthetic:p%2Bass@second.example.test:1080';
  f.controls.setCustomProxyInput(replacement);
  await f.controls.testAccountProxy();
  assert.equal(f.tested[0], replacement);
  f.modal.closeModal();
  assert.equal(f.saved.length, 0, 'closing does not mutate the account');
  assert.equal(f.value.value, '');
  f.modal.openCreateModal();
  assert.equal(f.controls.proxyMode.value, 'global');
  assert.equal(f.controls.customProxyInput.value, '');
}

(async() => {
  await testActualEditTestAndSave();
  await testModeToggleDoesNotWritePlaceholderBack();
  testFormFallbackAndMissingFullValue();
  testExplicitEmptyDetailsAndOtherModes();
  testAssetCacheBustTargetsOnlyEditedChunks();
  await testEditedUrlAndCancelIsolation();
  console.log('PASS: account proxy edit, test, save, mode switches and reset');
})().catch(error => {console.error(error); process.exitCode = 1;});
