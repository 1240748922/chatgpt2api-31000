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
for(const file of ['Accounts-CQrrBRkk.js','Dashboard-DoNiNtvp.js']) {
 const text = fs.readFileSync(path.join(assets,file),'utf8');
 assert(text.includes('import AccountAvailability'));
 assert(text.includes('(AccountAvailability)'));
}
console.log('PASS: readiness states, unknown/zero distinction, maintenance status and both page integrations');
