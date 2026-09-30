import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
import ts from '../apps/web/node_modules/typescript/lib/typescript.js';

const source=await readFile(new URL('../apps/web/src/lib/sha256.ts',import.meta.url),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText;
const {sha256Fallback,sha256Hex}=await import('data:text/javascript;base64,'+Buffer.from(compiled).toString('base64'));
const bytes=value=>new TextEncoder().encode(value).buffer;

test('pure TypeScript SHA-256 matches standard vectors',()=>{
  assert.equal(sha256Fallback(bytes('')),'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
  assert.equal(sha256Fallback(bytes('abc')),'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
});

test('SHA-256 works without crypto.subtle',async()=>{
  assert.equal(await sha256Hex(bytes('abc'),null),'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
});
