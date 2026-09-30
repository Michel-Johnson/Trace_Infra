import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import ts from '../apps/web/node_modules/typescript/lib/typescript.js';
const source=await readFile(new URL('../apps/web/src/plugins/slicers.ts',import.meta.url),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText;
const {operationMatch,facetMatch}=await import('data:text/javascript;base64,'+Buffer.from(compiled).toString('base64'));

test('operation and skill filters compose without changing source rows',()=>{
  const rows=[{span_id:'a',operation:'read',skill:{action:'load'},status:'ok',tool_ms:null},{span_id:'b',operation:'bash',skill:null,status:'error',tool_ms:1}];
  const before=structuredClone(rows);
  assert.deepEqual(rows.filter(r=>operationMatch(r,{types:['read'],skill:'any',status:'missing'})).map(r=>r.span_id),['a']);
  assert.deepEqual(rows,before);
});
test('classification values cannot collide with selection sentinel names',()=>{
  const assignments=new Map([['a',{status:'assigned',values:['unknown']}],['b',{status:'unknown',values:[]}],['c',{status:'not_applicable',values:[]}]]);
  assert.equal(facetMatch('a','value:unknown',assignments),true);
  assert.equal(facetMatch('a','unknown',assignments),false);
  assert.equal(facetMatch('b','unknown',assignments),true);
  assert.equal(facetMatch('c','unknown',assignments),false);
  assert.equal(facetMatch('c','not_applicable',assignments),true);
  assert.equal(facetMatch('d','unknown',undefined),true);
});
