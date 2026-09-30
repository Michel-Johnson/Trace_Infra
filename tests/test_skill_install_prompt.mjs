import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import ts from '../apps/web/node_modules/typescript/lib/typescript.js';

const source=await readFile(new URL('../apps/web/src/skills/install.ts',import.meta.url),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText;
const {buildSkillInstallPrompt,defaultSkillServer}=await import('data:text/javascript;base64,'+Buffer.from(compiled).toString('base64'));

test('default install prompt uses the deployed server and self-contained layout',()=>{
  const prompt=buildSkillInstallPrompt();
  assert.equal(defaultSkillServer,'http://10.37.24.3:8766');
  assert.match(prompt,/http:\/\/10\.37\.24\.3:8766\/api\/skills\/archive/);
  assert.match(prompt,/http:\/\/10\.37\.24\.3:8766\/api\/skills\/manifest/);
  assert.match(prompt,/http:\/\/10\.37\.24\.3:8766\/skills\.html/);
  assert.match(prompt,/trace-hunter-client\/manifest\.json/);
  assert.doesNotMatch(prompt,/VERSION\.json/);
  assert.match(prompt,/逐文件比较/);
  assert.match(prompt,/自带可执行 CLI/);
  assert.match(prompt,/authentication=none/);
});

test('future server address replacement keeps one canonical endpoint',()=>{
  const prompt=buildSkillInstallPrompt('https://trace.example.com/');
  assert.match(prompt,/https:\/\/trace\.example\.com\/api\/skills\/archive/);
  assert.match(prompt,/https:\/\/trace\.example\.com\/api\/skills\/manifest/);
  assert.match(prompt,/不要直接覆盖/);
  assert.match(prompt,/执行 capabilities/);
});
