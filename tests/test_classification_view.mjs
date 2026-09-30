import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import ts from '../apps/web/node_modules/typescript/lib/typescript.js';
const source=await readFile(new URL('../apps/web/src/plugins/classification-view.ts',import.meta.url),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText;
const {orderCalls,stageTiming,tokenLabel,turnLabel}=await import('data:text/javascript;base64,'+Buffer.from(compiled).toString('base64'));
const row=(id,ms,index)=>({row:{span_id:id,tool_ms:ms,response_ms:ms,cycle_ms:ms},index});
const assignment=value=>({status:'assigned',values:[value]});

test('duration sort keeps zero, moves missing last and preserves original indices',()=>{
  const rows=[row('missing',null,0),row('zero',0,1),row('slow',2000,2),row('tie',2000,3)];
  const before=structuredClone(rows);
  assert.deepEqual(orderCalls(rows,'tool_ms').map(x=>x.index),[2,3,1,0]);
  assert.deepEqual(rows,before);
  assert.deepEqual(orderCalls(orderCalls(rows,'tool_ms'),'source').map(x=>x.index),[0,1,2,3]);
});
test('phase grouping follows declared order, keeps repeated phases and unknown calls',()=>{
  const rows=[row('table1',3,0),row('plan',4,1),row('table2',2,2),row('unknown',null,3)];
  const values=new Map([['table1',assignment('table')],['table2',assignment('table')],['plan',assignment('plan')]]);
  assert.deepEqual(orderCalls(rows,'classification',values,[{id:'value:plan'},{id:'value:table'},{id:'unknown'}]).map(x=>x.index),[1,0,2,3]);
});

const span=(id,kind,start,end,extra={})=>({id,kind,phase_id:'task',agent_id:'main',request_id:kind==='model'?'req':null,attempt:1,start_ms:start,end_ms:end,duration_ms:null,operation:'bash',...extra});
const bundle=spans=>({run:{id:'run'},trace:{spans},view:{scope:['task']}});
const classification=values=>({value:'all',dimension:{id:'base.stage',values:[{id:'plan',title:'Plan'},{id:'table',title:'Table'}]},rows:{run:new Map(values)}});
test('time statistics deduplicate requests and exclude nested agents from workload',()=>{
  const data=bundle([span('m1','model',0,6000),span('m2','model',0,6000),span('a','agent',0,15000),span('t','tool',6000,8000),span('w','wait',8000,11000)]);
  const groups=stageTiming(data,classification([['m1',assignment('plan')],['m2',assignment('plan')],['t',assignment('table')]]));
  assert.equal(groups.find(g=>g.id==='value:plan').models.ms,6000);
  assert.equal(groups.find(g=>g.id==='value:plan').models.count,1);
  assert.equal(groups.find(g=>g.id==='value:table').tools.ms,2000);
  assert.equal(groups.find(g=>g.id==='unknown').waits.ms,3000);
});
test('conflicting model timing, cross-phase attribution and untimed tools stay explicit',()=>{
  const data=bundle([span('m1','model',0,6000),span('m2','model',1000,6000),span('t','tool',null,null),span('outside','tool',0,100,{phase_id:'export'})]);
  const groups=stageTiming(data,classification([['m1',assignment('plan')],['m2',assignment('table')]]));
  assert.equal(groups.find(g=>g.id==='shared').models.known,0);
  assert.equal(groups.find(g=>g.id==='unknown').tools.known,0);
  assert.equal(groups.find(g=>g.id==='unknown').tools.count,1);
});

const usage={input_tokens:1000,output_tokens:200,cache_read_tokens:800,cache_write_tokens:100,thinking_tokens:50};
test('step tokens deduplicate requests, retain cache subsets and count user turns once',()=>{
  const data=bundle([span('m1','model',0,6000,{usage}),span('m2','model',0,6000,{usage}),span('t1','tool',6000,7000),span('t2','tool',7000,8000)]);
  data.view.span_turns=Object.fromEntries(['m1','m2','t1','t2'].map(id=>[id,{turn_id:'user-1',ordinal:1,coverage:'complete'}]));
  const values=classification(['m1','m2','t1','t2'].map(id=>[id,assignment('plan')]));
  const [g]=stageTiming(data,values);
  assert.equal(g.models.count,1);assert.equal(g.tokens.total_tokens.value,1200);
  assert.equal(g.tokens.cache_read_tokens.value,800);assert.equal(g.tokens.thinking_tokens.value,50);
  assert.equal(g.turns.ids.length,1);assert.equal(g.turns.known,3);
  assert.equal(turnLabel(g.turns),'1 轮用户对话（第 1 轮）');
});
test('partial and conflicting usage never produces a fabricated complete total',()=>{
  const data=bundle([span('a','model',0,1000,{usage}),span('b','model',0,1000,{usage:{...usage,output_tokens:300}}),
    span('c','model',1000,2000,{request_id:'partial-in',usage:{...usage,output_tokens:null}}),
    span('d','model',2000,3000,{request_id:'partial-out',usage:{...usage,input_tokens:null}}),
    span('e','model',3000,4000,{request_id:'good',usage}),span('t','tool',4000,5000)]);
  const [g]=stageTiming(data,classification(data.trace.spans.map(s=>[s.id,assignment('table')])));
  assert.equal(g.models.count,4);assert.equal(g.tokenConflicts,1);
  assert.equal(g.tokens.total_tokens.value,1200);assert.equal(g.tokens.total_tokens.known,1);
  assert.equal(tokenLabel(g.tokens.total_tokens,g.models.count),'已记录 1,200');
  assert.equal(turnLabel(g.turns),'轮次未知');
});
test('scope excludes export usage and a model-only planning step is still returned',()=>{
  const data=bundle([span('m','model',0,1000,{usage}),span('export','model',1000,2000,{request_id:'export',phase_id:'export',usage})]);
  const [g]=stageTiming(data,classification([['m',assignment('plan')]]));
  assert.equal(g.tools.count,0);assert.equal(g.models.count,1);assert.equal(g.tokens.total_tokens.value,1200);
});

test('time share uses recorded concurrent workload and includes unknown classifications',()=>{
  const data=bundle([span('m','model',0,6000),span('copy','model',0,6000),span('t','tool',0,2000),span('w','wait',0,2000),span('missing','tool',null,null)]);
  const groups=stageTiming(data,classification([['m',assignment('plan')],['copy',assignment('plan')],['t',assignment('table')]]));
  assert.equal(groups.find(g=>g.id==='value:plan').timeShare,.6);
  assert.equal(groups.find(g=>g.id==='value:table').timeShare,.2);
  assert.equal(groups.find(g=>g.id==='unknown').timeShare,.2);
  assert.equal(groups.find(g=>g.id==='unknown').timeCoverage,.5);
  assert.equal(groups.reduce((n,g)=>n+g.timeShare,0),1);
});

test('zero timing, missing timing and an empty denominator are distinct',()=>{
  const data=bundle([span('zero','tool',0,0),span('timed','tool',0,1000),span('missing','tool',null,null)]);
  const groups=stageTiming(data,classification([['zero',assignment('plan')],['timed',assignment('table')]]));
  assert.equal(groups.find(g=>g.id==='value:plan').timeShare,0);
  assert.equal(groups.find(g=>g.id==='unknown').timeShare,null);
  assert.equal(groups.find(g=>g.id==='unknown').timeCoverage,0);
  const [zero]=stageTiming(bundle([span('zero','tool',0,0)]),classification([['zero',assignment('plan')]]));
  assert.equal(zero.timeShare,null);
  assert.equal(zero.timeCoverage,1);
});
