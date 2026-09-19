const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {Blob} = require('node:buffer');
const script = path.resolve(__dirname, '../web_dist/account-import.js');
const {parseInput} = require(script);

assert.deepEqual(parseInput('at-one\r\nrt.1.two'), [{access_token:'at-one'}, {refresh_token:'rt.1.two'}]);
assert.deepEqual(parseInput('opaque-refresh', 'rt'), [{refresh_token:'opaque-refresh'}]);
assert.deepEqual(parseInput('{"access_token":"at","refresh_token":"rt"}'), [{access_token:'at', refresh_token:'rt'}]);
assert.deepEqual(parseInput('{"credentials":{"accessToken":"at"}}'), [{credentials:{accessToken:'at'}}]);
assert.equal(parseInput('{"accounts":[{"refreshToken":"rt"}],"tokens":["at"],"refresh_tokens":["opaque"]}').length, 3);
assert.throws(() => parseInput('{broken-json'), /JSON 格式错误/);
assert.throws(() => parseInput('{"foo":"bar"}'), /缺少/);

async function testPage({unauthorized = false} = {}) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {value: id === 'mode' ? 'auto' : '', hidden: true, files: [], textContent: '', checked: false,
      addEventListener(name, fn) { this[name] = fn; }, replaceChildren() {}, append() {}});
    return elements.get(id);
  };
  let posted, getCalls = 0;
  const job = {id:'test-id', status:'completed', total:2, processed:2, saved:2, added:2, skipped:0,
    created_at:1, updated_at:2, done:true, synced:0, sync_failed:0};
  const context = {
    document: {getElementById: element, createElement: () => ({})}, Blob,
    localStorage: {getItem: key => key === 'chatgpt2api.adminKey' ? 'fake-shared-admin' : null, setItem() {}},
    crypto: {getRandomValues: array => array.fill(42)}, setTimeout: () => 1, clearTimeout() {},
    fetch: async (url, options) => {
      assert.equal(options.headers.Authorization, 'Bearer fake-shared-admin', 'reuse main console auth');
      if (unauthorized) return {status:401, ok:false};
      if (options.method === 'POST') { posted = JSON.parse(options.body); return {ok:true, json:async()=>({job})}; }
      getCalls++;
      const data = url.includes('/events?') ? {events:[{id:1,time:1,code:'batch_saved',start:1,end:2,saved:2,added:2,duration_ms:123}],next_cursor:1}
        : url.endsWith('/test-id') ? {job} : {jobs:posted ? [job] : []};
      return {ok:true, json:async()=>data};
    },
  };
  vm.runInNewContext(fs.readFileSync(script, 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  if (unauthorized) {
    assert.equal(element('login').hidden, false);
    assert.equal(element('submit').disabled, true);
    return;
  }
  assert.equal(element('submit').disabled, false);
  element('file').files = [
    {name:'first.json', size:30, text:async()=>'[{"access_token":"synthetic-at"}]'},
    {name:'second.txt', size:15, text:async()=>'rt.synthetic'},
  ];
  await element('submit').click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(posted.accounts.length, 2);
  assert.equal(posted.accounts[1].refresh_token, 'rt.synthetic');
  assert(posted.request_key);
  assert.match(element('logs').textContent, /耗时 0.12 秒/);
  assert.match(element('saved').textContent, /2 \/ 2/);
  assert(getCalls >= 3);
}
(async () => { await testPage(); await testPage({unauthorized:true}); console.log('account import UI: parsing, multi-file submission, shared login, progress/logs passed'); })().catch(error => {console.error(error);process.exitCode=1;});
