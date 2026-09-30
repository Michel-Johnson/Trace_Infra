import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import ts from '../apps/web/node_modules/typescript/lib/typescript.js';
async function load(path){
  const source=await readFile(new URL(path,import.meta.url),'utf8');
  const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText;
  return import('data:text/javascript;base64,'+Buffer.from(compiled).toString('base64'));
}
const {readBatchHistory}=await load('../apps/web/src/api/batch-history.ts');
const {latestClassifications}=await load('../apps/web/src/plugins/classification-results.ts');

test('client reads past one hundred recent batches to retain the old successful Case artifact',async()=>{
  const jobs=(id,state)=>[{id:'job-'+id,run_id:'run-a',state,input_digest:'digest',scope:'task',config:{},snapshot_digest:'selection',
    result:state==='completed'?{plugin_ref:{id:'official.base-stages',version:'1.2.0'},contribution_id:'classify',scope:'task',
      input_ref:{document_id:'run-a',document_digest:'digest',selection_digest:'selection'},output:{kind:'facets',data:{facets:[],assignments:[]}}}:null}];
  const batches=Array.from({length:102},(_,i)=>({id:String(102-i).padStart(3,'0'),created_at:'2026-09-11T00:00:00Z',
    scope:'task',config:{},plugin_id:'official.base-stages',plugin_version:'1.2.0',contribution_id:'classify',output_kind:'facets',
    jobs:jobs(i,i===101?'completed':'queued')}));
  const requests=[];
  const all=await readBatchHistory(async(cursor,limit)=>{
    requests.push({cursor,limit});
    const offset=cursor?batches.findIndex(b=>b.id===cursor)+1:0;
    return batches.slice(offset,offset+limit);
  });
  assert.equal(requests.length,2);assert.equal(requests[0].limit,100);assert.equal(all.length,102);
  const [context]=latestClassifications(all,[{run:{id:'run-a'},digest:'digest'}],'task');
  assert.equal(context.jobs.length,1);assert.equal(context.jobs[0].id,'job-101');assert.equal(context.counts.queued,1);
});

test('history pages deduplicate IDs and a cancelled fetch never returns a partial list',async()=>{
  const first=Array.from({length:100},(_,i)=>({id:String(i)}));
  const all=await readBatchHistory(async cursor=>cursor?[first.at(-1),{id:'100'}]:first);
  assert.equal(all.length,101);
  const controller=new AbortController();let calls=0;
  await assert.rejects(readBatchHistory(async()=>{calls++;controller.abort();return first;},controller.signal),{name:'AbortError'});
  assert.equal(calls,1);
  await assert.rejects(readBatchHistory(async()=>{throw new Error('must not fetch');},controller.signal),{name:'AbortError'});
});

test('a non-progressing cursor reports failure instead of hanging or silently truncating',async()=>{
  const first=Array.from({length:100},(_,i)=>({id:String(i)}));
  let calls=0;
  await assert.rejects(readBatchHistory(async()=>{calls++;return first;}),/游标未推进/);
  assert.equal(calls,2);
});
