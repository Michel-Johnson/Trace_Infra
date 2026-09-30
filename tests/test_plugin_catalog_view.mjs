import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import ts from '../apps/web/node_modules/typescript/lib/typescript.js';

const source=await readFile(new URL('../apps/web/src/plugins/catalog-view.ts',import.meta.url),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText;
const {comparePluginVersions,extensionVersionKey,groupPluginCatalog,pluginMatches}=await import('data:text/javascript;base64,'+Buffer.from(compiled).toString('base64'));
const entry=(id,version,kind='slicer',source='native')=>({source,manifest:{plugin_id:id,version,title:'Base 分类',description:'按阶段和 CLI 筛选',contributes:[{kind}]}});

test('one card per plugin ID keeps every historical source/version without mutating catalog',()=>{
  const items=[entry('base','1.2.0'),entry('base','1.10.0'),entry('base','1.3.0'),entry('time','1.0.0','evaluator'),entry('base','1.3.0','slicer','evaluation_v1')];
  const before=structuredClone(items),groups=groupPluginCatalog(items);
  assert.equal(groups.length,2);
  assert.equal(groups[0].latest.manifest.version,'1.10.0');
  assert.equal(groups[0].versions.length,4);
  assert.equal(new Set(groups[0].versions.map(extensionVersionKey)).size,4);
  assert.deepEqual(items,before);
});

test('released versions outrank prereleases and prerelease numeric identifiers sort numerically',()=>{
  const versions=['1.1.0-rc.9','1.1.0-rc.10','1.1.0','1.0.9','1.10.0','1.2.0'];
  assert.deepEqual([...versions].sort((a,b)=>comparePluginVersions(b,a)),['1.10.0','1.2.0','1.1.0','1.1.0-rc.10','1.1.0-rc.9','1.0.9']);
  assert.equal(comparePluginVersions('1.0.0+build.2','1.0.0+build.1'),0);
  const [group]=groupPluginCatalog([entry('same','1.0.0','slicer','evaluation_v1'),entry('same','1.0.0')]);
  assert.equal(group.latest.source,'native');assert.equal(group.versions.length,2);
});

test('capability counts describe displayed versions and search composes with capability filters',()=>{
  const [group]=groupPluginCatalog([entry('official.base','1.0.0','renderer'),entry('official.base','2.0.0','slicer')]);
  assert.equal(pluginMatches(group,'renderer',''),false);
  assert.equal(pluginMatches(group,'slicer','CLI'),true);
  assert.equal(pluginMatches(group,'all','official.BASE'),true);
  assert.equal(pluginMatches(group,'evaluator','base'),false);
});
