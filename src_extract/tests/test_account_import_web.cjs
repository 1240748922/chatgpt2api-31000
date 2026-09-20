const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {Blob} = require('node:buffer');
const assets = path.resolve(__dirname, '../web_dist/assets');
const source = fs.readFileSync(path.join(assets, 'accountImportRuntime-v3.js'), 'utf8');
const context = {Blob, setTimeout, clearTimeout, Date};
vm.createContext(context);
vm.runInContext(source.replace(/export /g, '') + '\nthis.runtime={parseInput,readImportInputs,createImportController,formatEvent,collectImportItems};', context);
const {parseInput, readImportInputs, createImportController, formatEvent, collectImportItems} = context.runtime;
const plain = object => JSON.parse(JSON.stringify(object));
const flush = () => new Promise(resolve => setImmediate(resolve));

async function testInputs() {
  assert.deepEqual(plain(parseInput('at-one\r\n# note\nrt.1.two')), [{access_token:'at-one'}, {refresh_token:'rt.1.two'}]);
  assert.deepEqual(plain(parseInput('opaque-refresh', 'refresh_token')), [{refresh_token:'opaque-refresh'}]);
  assert.deepEqual(plain(parseInput('{"credentials":{"accessToken":"at"}}')), [{access_token:'at'}]);
  assert.equal(parseInput('{"data":{"items":[{"refreshToken":"rt"}]}}')[0].refresh_token, 'rt');
  assert.deepEqual(plain(parseInput('{"type":"codex","email":"person@example.com","access_token":"at","refresh_token":"rt"}', 'session_json')), [{access_token:'at',refresh_token:'rt'}]);
  assert.deepEqual(plain(parseInput('{"email":"person@example.com","accessToken":"at","refreshToken":"rt"}', 'access_token')), [{access_token:'at'}]);
  assert.deepEqual(plain(parseInput('{"email":"person@example.com","accessToken":"at","refreshToken":"rt"}', 'refresh_token')), [{refresh_token:'rt'}]);
  assert.equal(parseInput('{"accounts":[{"access_token":"at"}],"tokens":["at2"],"refresh_tokens":["opaque"]}').length, 3);
  assert.throws(() => parseInput('{broken-json'), /JSON 格式错误/);
  assert.throws(() => parseInput('{"foo":"bar"}'), /缺少/);
  assert.throws(() => parseInput('{"refresh_tokens":[{}]}'), /无效 RT/);
  const itemEvents = collectImportItems([{id:7,code:'quota_batch',items:[{account_label:'person@example.com',status:'failed',stage:'quota',message:'auth_invalid'}]}]);
  assert.equal(itemEvents[0].account_label, 'person@example.com');
  assert.equal(itemEvents[0].status_label, '失败');
  assert.match(formatEvent({time:1,code:'refresh_failed',item:2,account_label:'person@example.com',error_code:'refresh_token_invalid',duration_ms:10}), /person@example.com/);
  const files = [{name:'first.json', size:50, text:async()=>'[{"auth":{"accessToken":"synthetic-at"},"group_id":"source-group","proxy":"direct"}]'},
    {name:'second.json', size:30, text:async()=>'[{"refresh_token":"rt.synthetic"}]'}];
  const parsed = await readImportInputs({text:'', files, mode:'cpa_json'});
  assert.equal(parsed.length, 2);
  assert.equal(parsed[0].source_type, 'codex');
  assert.equal(parsed[0].group_id, 'source-group');
  assert.equal(parsed[0].proxy, 'direct');
  assert.equal(parsed[1].refresh_token, 'rt.synthetic');
  assert.equal((await readImportInputs({files:[{name:'a.txt',size:5,text:async()=>'rt.s'}], mode:'refresh_token'}))[0].refresh_token, 'rt.s');
  const sessionFile = await readImportInputs({files:[{name:'session.json',size:120,text:async()=>'{"type":"codex","email":"person@example.com","access_token":"file-at","account_id":"id"}'}], mode:'session_json'});
  assert.deepEqual(plain(sessionFile), [{access_token:'file-at',source_type:'web'}]);
  const refreshFile = await readImportInputs({files:[{name:'session.json',size:120,text:async()=>'{"email":"person@example.com","accessToken":"file-at","refreshToken":"file-rt"}'}], mode:'refresh_token'});
  assert.deepEqual(plain(refreshFile), [{refresh_token:'file-rt',source_type:'web'}]);
  await assert.rejects(readImportInputs({files:[{name:'a.json',size:100000000,text:async()=>{throw new Error('must not read');}}]}), /64 MiB/);
  assert.match(formatEvent({time:1,code:'batch_saved',start:1,end:2,saved:2,added:2,duration_ms:123}), /0.12 秒/);
}

function fixture(options={}) {
  let state, changed=0, counter=0;
  const scheduled = new Map(), saved = new Map(), posts=[];
  const job = {id:'job-one', status:'saving', total:2, processed:1, saved:1, added:1, skipped:0,
    created_at:1, updated_at:2, done:false, synced:0, sync_failed:0};
  const api = {
    get: async url => url.includes('/events?') ? {events:[{id:1,time:1,code:'batch_saved',start:1,end:1,saved:1,added:1,duration_ms:123}],next_cursor:1}
      : url.endsWith('/job-one') ? {job} : {jobs:[job]},
    post: async (url, body) => {posts.push({url,body});return {job};},
  };
  const controller = createImportController({api, onUpdate:value=>{state=value;},onAccountsChanged:()=>{changed++;},
    storage:{getItem:key=>saved.get(key), setItem:(key,value)=>saved.set(key,value)},
    schedule:fn=>{scheduled.set(++counter,fn);return counter;},cancel:id=>scheduled.delete(id),
    requestKey:()=>`request-${++counter}`, ...options});
  return {controller, api, job, scheduled, saved, posts, state:()=>state, changed:()=>changed};
}

async function testSubmissionAndResume() {
  const f=fixture();
  await f.controller.history();
  assert.equal(f.state().selected,'job-one');
  assert.equal(f.scheduled.size,1);
  assert.equal(f.state().events.length,1);
  await f.controller.submit({accounts:[{access_token:'synthetic'}],targetGroupId:'chosen-group'});
  await flush();
  assert.equal(f.posts[0].url,'/api/account-import-jobs');
  assert.equal(f.posts[0].body.target_group_id,'chosen-group');
  assert.equal(f.posts[0].body.sync_after_import,true);
  assert(f.posts[0].body.request_key);
  assert.equal(f.saved.get('chatgpt2api.importJob'),'job-one');
  assert(!JSON.stringify([...f.saved]).includes('synthetic'));
  assert.equal(f.state().busy,false, 'a running background job must allow another import');
  f.controller.stop();
  assert.equal(f.scheduled.size,0);
  f.job.status='completed';f.job.done=true;f.job.saved=2;f.job.processed=2;
  f.controller.resume();await flush();
  assert.equal(f.state().job.status,'completed');
  assert.equal(f.scheduled.size,0);
  assert(f.changed() > 0, 'refresh the account list after inserts and completion');
  f.controller.stop();
}

async function testRetryKeepsRequestKeyAndOldPollsCannotReplaceSelection() {
  const f=fixture();
  let first=true;
  f.api.post=async(url,body)=>{f.posts.push({url,body});if(first){first=false;throw new Error('network interrupted');}return {job:f.job};};
  const args={accounts:[{access_token:'synthetic'}],syncAfterImport:false,targetGroupId:''};
  assert.equal(await f.controller.submit(args),true);
  assert.equal(f.posts.length,2);
  assert.equal(f.posts[0].body.request_key,f.posts[1].body.request_key);
  assert.equal(f.posts[1].body.target_group_id,'');
  await flush();
  let finish;
  f.api.get=url=>new Promise(resolve=>{if(url.includes('/events?'))resolve({events:[],next_cursor:0});else finish=resolve;});
  const reading=f.controller.select('job-one');
  f.controller.stop();
  finish({job:{...f.job,status:'failed'}});
  await reading;
  assert.notEqual(f.state().job.status,'failed');
  assert.equal(f.scheduled.size,0);
}

async function testBackgroundSubmitDoesNotWaitForFirstPoll() {
  const f = fixture();
  const pending = [];
  f.api.get = () => new Promise(resolve => pending.push(resolve));
  const submission = f.controller.submit({accounts:[{access_token:'synthetic'}],syncAfterImport:false,targetGroupId:''});
  let timeout;
  const deadline = new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error('submit waited for the first background poll')), 250); });
  try {
    assert.equal(await Promise.race([submission, deadline]), true);
  } finally { clearTimeout(timeout); }
  assert.equal(f.state().busy, false);
  assert.equal(f.state().job.id, 'job-one');
  f.controller.stop();
}

async function testLogPagingAndReconnection() {
  const f=fixture();let page=0;
  f.job.done=true;f.job.status='completed';
  f.api.get=async url=>url.includes('/events?')
    ? {events:page++===0 ? Array.from({length:100},(_,i)=>({id:i+1,time:1,code:'completed'})) : [{id:101,time:1,code:'completed'}],next_cursor:page===1?100:101}
    : {job:f.job};
  await f.controller.select('job-one');
  assert.equal(f.scheduled.size,1, 'drain logs even if job is completed');
  const next=[...f.scheduled.values()][0];f.scheduled.clear();await next();
  assert.equal(f.state().events.length,101);
  assert.equal(f.scheduled.size,0);
  f.api.get=async()=>{throw Object.assign(new Error('expired login'),{response:{status:401}});};
  await f.controller.select('job-one');
  assert.equal(f.scheduled.size,0,'do not poll forever after logout');
  assert.equal(f.state().connection,'expired login');
  f.controller.stop();
}

async function testTransientGatewayErrorRetries() {
  const f = fixture();
  let first = true;
  f.api.get = async url => {
    if (first) {
      first = false;
      throw Object.assign(new Error('Bad Gateway'), {response:{status:502}});
    }
    return url.includes('/events?') ? {events:[],next_cursor:0} : {job:f.job};
  };
  await f.controller.select('job-one');
  assert.equal(f.state().connection, '连接暂时中断，正在自动重试…');
  assert.equal(f.scheduled.size, 1, 'temporary gateway failures keep polling');
  f.controller.stop();
}

async function testOriginalModalAndBackupRestore() {
  const bundle=fs.readFileSync(path.join(assets,'Accounts-CQrrBRkk.js'),'utf8');
  const panel=fs.readFileSync(path.join(assets,'LocalAccountImportPanel-v3.js'),'utf8');
  assert(bundle.includes('localImportModes.includes(t($))?i(LocalAccountImportPanel'));
  assert(bundle.includes('key:`local-account-import-${t($)}`'));
  assert(bundle.includes('targetGroupId:Gt.value'));
  assert(bundle.includes('onAccountsChanged:()=>t(Zt)({silentErrorToast:!0})'));
  assert(bundle.includes('value:"refresh_token"'));
  assert(bundle.includes('t($)==="oauth_login"?'));
  assert(bundle.includes('t($)==="backup_json"?'));
  assert(bundle.includes('t($)==="remote_cpa"?'));
  assert(bundle.includes('t($)==="sub2api"?'));
  assert(!panel.includes('onActivated(controller.resume)'));
  assert(!panel.includes('onDeactivated(controller.stop)'));
  assert(panel.includes('event?.target'));
  assert(!panel.includes('onInput: onFileChange'), 'file input must not process the same FileList twice');
  assert(panel.includes('onChange: onFileChange'));
  assert(panel.includes('JSON.stringify(accounts, null, 2)'));
  assert(panel.includes('内容已填入上方输入框'));
  assert(bundle.includes('at=R(()=>st.value)'), 'background import state must not lock the import modal');
  assert(!bundle.includes('s.value||(o.value=!1)'), 'closing the import modal must not be blocked by an import flag');
  assert(bundle.includes('scrollable:"",onClose:t(ao)'), 'modal overlay close must use the same close handler');
  assert(bundle.includes('title:"导入账号",compact:"",onClose:t(ao)'), 'modal close button must remain enabled');
  const calls=[];
  const bulk={start:async()=>{},update:()=>{},appendEvents:()=>{},finish:()=>{},end:()=>{},refreshProgress:{value:{}},batchBusy:{value:false}};
  const scope={T:value=>({value}),De:()=>({}),Ke:()=>({ask:async()=>true}),Vo:[],Ks:()=>({}),Lo:async()=>{},
    Q:{importAccounts:async(accounts,source,options)=>{calls.push({accounts,source,options});return {added:1,skipped:0,updated_ids:[],events:[]};}},
    Jo:{finishOAuthLogin:async(...args)=>{calls.push({oauth:args});return {added:1,skipped:0,updated_ids:[],events:[]};}}};
  vm.createContext(scope);
  vm.runInContext(bundle.slice(bundle.indexOf('function bs('),bundle.indexOf('function on(e)')),scope);
  const model=scope.tn({bulkProgress:bulk,loadData:async()=>{},setError:(_,error)=>{throw error;},normalizeErrorMessage:error=>error.message});
  model.importMode.value='backup_json';
  await model.importLocalAccountFiles([{name:'backup.json',text:async()=>'[{"access_token":"synthetic","quota":0,"status":"禁用","proxy":"direct","group_id":"original"}]'}]);
  assert.equal(calls[0].options.restore,true,'backup keeps full restore semantics');
  assert.equal(calls[0].options.syncAfterImport,false);
  assert.equal(calls[0].accounts[0].status,'禁用');
  assert.equal(calls[0].accounts[0].quota,0);
  model.oauthSessionId.value='session';model.oauthCallbackText.value='code';model.importTargetGroupValue.value='chosen';
  await model.finishOAuthLogin();
  assert.deepEqual(plain(calls[1].oauth),['session','code','chosen']);
}

(async()=>{
  await testInputs();await testSubmissionAndResume();await testRetryKeepsRequestKeyAndOldPollsCannotReplaceSelection();await testBackgroundSubmitDoesNotWaitForFirstPoll();
  await testLogPagingAndReconnection();await testTransientGatewayErrorRetries();await testOriginalModalAndBackupRestore();
  console.log('PASS: original modal integration, AT/RT/JSON files, target groups, async progress/logs, resume/idempotency, backup/OAuth compatibility');
})().catch(error=>{console.error(error);process.exitCode=1;});
