import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import ts from '../apps/web/node_modules/typescript/lib/typescript.js';
async function module(name) {
  const source=await readFile(new URL(`../apps/web/src/plugins/${name}.ts`,import.meta.url),'utf8');
  const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText;
  return import('data:text/javascript;base64,'+Buffer.from(compiled).toString('base64'));
}
const {pageEntries,recordedTurns,statusMatches,turnMatches}=await module('trace-window');
const {orderCalls}=await module('classification-view');
const {readDrilldown,drilldownMatch}=await module('drilldown');
const {operationMatch,facetMatch}=await module('slicers');

test('a thousand calls remain reachable in bounded pages, including missing-time tail records',()=>{
  const rows=Array.from({length:1000},(_,index)=>({index,row:{span_id:`tool-${index}`,tool_ms:index===999?null:index}}));
  const sorted=orderCalls(rows,'tool_ms'),seen=[];
  for(let page=1;page<=Math.ceil(rows.length/48);page++) {
    const window=pageEntries(sorted,page);
    assert.ok(window.entries.length<=48);
    seen.push(...window.entries.map(item=>item.row.span_id));
  }
  assert.equal(seen.length,1000);assert.equal(new Set(seen).size,1000);
  assert.equal(seen.at(-1),'tool-999');
  assert.equal(pageEntries(sorted,Infinity).page,1);
  assert.equal(pageEntries(sorted,99).page,21);
});

test('filtering and pagination preserve the original detail target and source ordering',()=>{
  const rows=Array.from({length:431},(_,index)=>({index,row:{span_id:`tool-${index}`,tool_ms:index%7===0?null:431-index,status:index%3===0?'error':'ok',operation:'bash'}}));
  const selected=rows[300];
  const filtered=rows.filter(item=>operationMatch(item.row,{types:[],skill:'all',status:'error'}));
  const sorted=orderCalls(filtered,'tool_ms');
  const pageNumber=Math.floor(sorted.indexOf(selected)/48)+1;
  const visible=pageEntries(sorted,pageNumber).entries.find(item=>item.row.span_id===selected.row.span_id);
  assert.strictEqual(visible,selected);assert.equal(visible.index,300);
  const restored=orderCalls(sorted,'source');
  assert.deepEqual(restored.map(item=>item.index),filtered.map(item=>item.index));
  assert.equal(pageEntries(filtered.slice(0,3),9).page,1);
  assert.deepEqual(pageEntries([],9).entries,[]);
});

test('multi-turn selection uses message associations rather than number of source files',()=>{
  const data={trace:{sources:Array.from({length:20},(_,i)=>({id:`source-${i}`}))},view:{rows:[{span_id:'a'},{span_id:'b'},{span_id:'c'},{span_id:'d'}],span_turns:{
    a:{turn_id:'turn-a',ordinal:1,coverage:'complete'},b:{turn_id:'turn-b',ordinal:2,coverage:'partial'},c:{turn_id:'not-observed',ordinal:3,coverage:'missing'},
  }}};
  assert.deepEqual(recordedTurns([data]),[1,2]);
  assert.deepEqual(data.view.rows.filter(row=>turnMatches(data,row,'2')).map(row=>row.span_id),['b']);
  assert.deepEqual(data.view.rows.filter(row=>turnMatches(data,row,'unknown')).map(row=>row.span_id),['c','d']);
  assert.equal(data.view.rows.filter(row=>turnMatches(data,row,'all')).length,4);
});

test('batch drilldown retains combined stage, intent, CLI and recorded status constraints',()=>{
  const params=new URLSearchParams({classifier:'official.base-stages@1.2.0:classify',stage:'permission',intent:'update',cli:'lark-cli base +role-update',status:'error'});
  const request=readDrilldown('?'+params);
  assert.equal(request.classifier,'official.base-stages@1.2.0:classify');
  assert.deepEqual(request.constraints.map(c=>[c.id,c.value]),[['base.stage','value:permission'],['base.intent','value:update'],['base.cli','value:lark-cli base +role-update']]);
  const assignments={
    'base.stage':new Map([['a',{status:'assigned',values:['permission']}],['b',{status:'assigned',values:['table']}]]),
    'base.intent':new Map([['a',{status:'assigned',values:['update']}],['b',{status:'assigned',values:['update']}]]),
    'base.cli':new Map([['a',{status:'assigned',values:['lark-cli base +role-update']}],['b',{status:'assigned',values:['lark-cli base +role-update']}]]),
  };
  const rows=[{span_id:'a',status:'error',operation:'bash'},{span_id:'b',status:'error',operation:'bash'},{span_id:'unclassified',status:'error',operation:'bash'}];
  const filtered=rows.filter(row=>operationMatch(row,{types:[],skill:'all',status:'all'})&&statusMatches(row,request.status)&&request.constraints.every(c=>facetMatch(row.span_id,c.value,assignments[c.id])));
  assert.deepEqual(filtered.map(row=>row.span_id),['a']);
  for(const status of ['ok','unknown','running'])assert.equal(statusMatches({status},status),true);
});

test('aggregate unknown, unclassified and shared CLI buckets drill into their actual records',()=>{
  const assignments=new Map([
    ['unknown',{status:'unknown',values:[]}],
    ['inapplicable',{status:'not_applicable',values:[]}],
    ['single',{status:'assigned',values:['read']}],
    ['shared',{status:'assigned',values:['read','write']}],
  ]);
  const selection=param=>readDrilldown('?cli='+param).constraints[0].value;
  for(const id of ['unknown','inapplicable','absent'])assert.equal(drilldownMatch(id,selection('__unknown__'),assignments),true);
  assert.equal(drilldownMatch('single',selection('__unknown__'),assignments),false);
  assert.equal(drilldownMatch('unknown',selection('__unknown__'),undefined),false);
  assert.equal(drilldownMatch('any',selection('__unclassified__'),undefined),true);
  assert.equal(drilldownMatch('absent',selection('__unclassified__'),new Map()),false);
  assert.equal(drilldownMatch('shared',selection('__multiple__'),assignments),true);
  assert.equal(drilldownMatch('single',selection('__multiple__'),assignments),false);
  assert.equal(drilldownMatch('shared',selection('__multiple__'),undefined),false);
  assert.equal(drilldownMatch('shared',selection('read'),assignments),true);
});
