const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assets = path.resolve(__dirname, '../web_dist/assets');
const code = fs.readFileSync(path.join(assets, 'accountAvailabilityRuntime-v1.js'), 'utf8');
const context = vm.createContext({});
vm.runInContext(code.replace(/export /g,''), context);
const display = context.availabilityDisplay;
assert.equal(display(null).generation,'--');
assert.equal(display({counts:{ready:0}}).states[0].value,'0');
for (const value of [-1,NaN,'7',null]) assert.equal(display({counts:{ready:value}}).states[0].value,'--');
const data = {counts:{ready:1000,unknown:50},generation_candidates:800,edit_candidates:780,
 maintenance:{mode:'normal',batch_size:2,active_images:300,reasons:['性能正常']}};
assert.equal(display(data).states[1].value,'50');
assert.equal(display(data).generation,'800');
assert.equal(display(data).edits,'780');
assert.equal(display(data).mode,'正常同步');
assert.equal(display(data).active,'300');
assert.equal(display({...data,snapshot_age_seconds:31}).stale,true);
assert.equal(display(data).generationQuota.known,'--', 'old API must not look like zero quota');
assert.equal(display(null).editQuota.unknown,'--');
for (const value of [-1,NaN,'7',null,Infinity,Number.MAX_SAFE_INTEGER+1]) {
 assert.equal(display({generation_quota:{known_remaining:value}}).generationQuota.known,'--');
}
const quotaData = {...data,generation_quota:{known_remaining:24000,known_accounts:7900,unknown_accounts:2,unlimited_accounts:3},
 edit_quota:{known_remaining:0,known_accounts:0,unknown_accounts:0,unlimited_accounts:0}};
assert.equal(display(quotaData).generationQuota.known,'24,000');
assert.equal(display(quotaData).generationQuota.unknown,'2');
assert.equal(display(quotaData).generationQuota.unlimited,'3');
assert.equal(display(quotaData).generationQuota.separate,true);
assert.equal(display(quotaData).editQuota.known,'0');
assert.equal(display(quotaData).editQuota.separate,false);
assert.equal(display({generation_quota:{unknown_accounts:'2'}}).generationQuota.separate,false);
for(const file of ['Accounts-CQrrBRkk.js','Dashboard-DoNiNtvp.js']) {
 const text = fs.readFileSync(path.join(assets,file),'utf8');
 assert(text.includes('import AccountAvailability'));
 assert(text.includes('(AccountAvailability)'));
}
// Cache bumps must never split Vue into multiple runtime URL instances.
const runtimePattern = /index-BhEm-7EJ\.js(?:\?v=[A-Za-z0-9-]+)?/g;
const runtimeUrls = new Set();
for (const file of fs.readdirSync(assets).filter(file=>file.endsWith('.js'))
 .map(file=>path.join(assets,file)).concat([path.join(assets,'../index.html'),path.join(assets,'../replenishment-nav.js')])) {
 for (const url of fs.readFileSync(file,'utf8').match(runtimePattern) || []) runtimeUrls.add(url);
}
assert.equal(runtimeUrls.size,1, 'all modules and the HTML entry must share the same Vue runtime cache version');
assert([...runtimeUrls][0].includes('?v='));
console.log('PASS: readiness states, ready quota/unknown/unlimited distinction, maintenance status and both page integrations');
