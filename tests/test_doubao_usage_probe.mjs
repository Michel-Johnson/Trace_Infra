import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const template=fs.readFileSync(new URL('../scripts/doubao_usage_probe.js',import.meta.url),'utf8');
for (const fail of [false,true]) {
  let calls=0; const timers=new Map();
  const response={code:0,data:{entries:[{usage:{quota_source:{display_text:'0.01%'}}}]}};
  const api={M:{AGWGetUsageTimeline:async params=>{calls++; assert.deepEqual(JSON.parse(JSON.stringify(params)),{page_size:20}); if(fail)throw Error('secret-error-details'); return response;}}};
  const require=id=>{assert.equal(id,359531);return api;};require.m={359531:()=>{}};
  const chunks=[];chunks.push=entry=>{Array.prototype.push.call(chunks,entry);entry[2](require);};
  const context=vm.createContext({window:{runtime:chunks},setTimeout:fn=>{timers.set(1,fn);return 1;}});
  const code=template.replace('__PROBE_KEY__','"owned"').replace('__CHUNK_KEY__','"runtime"').replace('__API_MODULE__','359531');
  vm.runInContext(code,context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls,1);assert.equal(chunks.length,0,'temporary chunk entry removed');
  assert.equal(context.owned.done,true);
  if(fail) {assert.equal(context.owned.error_type,'Error');assert.ok(!JSON.stringify(context.owned).includes('secret-error-details'));}
  else {assert.equal(context.owned.response,response);assert.equal(context.owned.response.data.entries[0].usage.quota_source.display_text,'0.01%');}
  timers.get(1)();assert.equal(context.owned,undefined,'automatic cleanup removes probe');
}
console.log('Usage probe: one read-only request, raw response preserved, error details omitted, automatic cleanup passed.');
