import test, {after} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, readFile, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import ts from '../apps/web/node_modules/typescript/lib/typescript.js';

const directory = await mkdtemp(join(tmpdir(), 'trace-hunter-browser-host-'));
after(() => rm(directory, {recursive: true, force: true}));
const resolve = createRequire(new URL('../apps/web/package.json', import.meta.url)).resolve;
for (const file of ['contracts', 'runtime']) {
  const source = await readFile(new URL(`../apps/web/src/plugins/host/${file}.ts`, import.meta.url), 'utf8');
  const output = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022}}).outputText
    .replace(/from ['"](@deepseek-ai\/[^'"]+)['"]/g, (_, name) => `from ${JSON.stringify(pathToFileURL(resolve(name)).href)}`)
    .replace(/from ['"]\.\/contracts['"]/g, 'from "./contracts.mjs"');
  await writeFile(join(directory, file + '.mjs'), output);
}
const {BrowserPluginHost} = await import(pathToFileURL(join(directory, 'runtime.mjs')));
const official = JSON.parse(await readFile(new URL('../plugins/extensions/call-activity/manifest.json', import.meta.url)));
const manifest = (id = official.plugin_id) => ({...structuredClone(official), plugin_id: id});
const serviceManifest = id => {const m = manifest(id); m.contributes = m.contributes.filter(c => c.kind !== 'renderer'); return m;};
const catalog = m => ({manifest: structuredClone(m), manifest_digest: '0'.repeat(64), source: 'native',
  runtimes: {grid: 'browser', list: 'browser', operations: 'browser', facets: 'browser', classify: 'builtin'}});
const key = m => JSON.stringify([m.plugin_id, m.version]);
const definition = (m, activate = c => {c.registerView('grid', () => 'grid'); c.registerView('list', () => 'list');}) => ({manifest: m, activate});

test('duration renderer upgrade defaults to latest and keeps legacy selection isolated', async t => {
  const latest = JSON.parse(await readFile(new URL('../plugins/extensions/call-activity-1.1.0/manifest.json', import.meta.url)));
  const host = new BrowserPluginHost([definition(latest), definition(official)]);
  t.after(() => host.dispose());
  await host.reconcile([catalog(official), catalog(latest)]);
  assert.equal(host.getSnapshot().selectedRenderer, key(latest));
  assert.ok(host.getSnapshot().views.every(view => view.owner === key(latest)));
  await host.selectRenderer(key(official));
  assert.ok(host.getSnapshot().views.every(view => view.owner === key(official)));
  await host.reconcile([catalog(official), catalog(latest)]);
  assert.equal(host.getSnapshot().selectedRenderer, key(official));
});

test('enable/disable cleans registrations and preserves the immutable manifest', async t => {
  const m = manifest(), before = structuredClone(m); let disposed = 0;
  const host = new BrowserPluginHost([definition(m, c => {
    c.effect(() => () => {disposed++;}); c.registerView('grid', () => 'grid');
  })]); t.after(() => host.dispose());
  await host.reconcile([catalog(m)]);
  assert.equal(host.getSnapshot().plugins[0].state, 'active');
  assert.equal(host.getSnapshot().views.length, 1);
  await host.setEnabled(key(m), false);
  assert.equal(host.getSnapshot().plugins[0].state, 'disabled');
  assert.equal(host.getSnapshot().views.length, 0); assert.equal(disposed, 1);
  await host.setEnabled(key(m), true);
  assert.equal(host.getSnapshot().views.length, 1); assert.deepEqual(m, before);
});

test('changed version/digest or removed catalog withdraws a view without static fallback', async t => {
  const m = manifest(), host = new BrowserPluginHost([definition(m)]); t.after(() => host.dispose());
  await host.reconcile([catalog(m)]); assert.equal(host.getSnapshot().views.length, 2);
  const incompatible = catalog(m); incompatible.manifest.package_digest = 'f'.repeat(64);
  await host.reconcile([incompatible]);
  assert.equal(host.getSnapshot().views.length, 0); assert.equal(host.getSnapshot().plugins[0].state, 'unavailable');
  await host.reconcile([catalog(m)]); assert.equal(host.getSnapshot().views.length, 2);
  await host.reconcile([]); assert.equal(host.getSnapshot().views.length, 0);
});

test('server runtime availability and source identity are enforced', async t => {
  const m = manifest(), host = new BrowserPluginHost([definition(m)]); t.after(() => host.dispose());
  const entry = catalog(m); entry.runtimes.list = 'unavailable';
  await host.reconcile([entry]); assert.deepEqual(host.getSnapshot().views.map(v => v.id), ['trace.grid']);
  entry.source = 'evaluation_v1';
  await host.reconcile([entry]); assert.equal(host.getSnapshot().views.length, 0);
});

test('activation failure rolls back partial registration and can be retried', async t => {
  const m = manifest(); let broken = true;
  const host = new BrowserPluginHost([definition(m, c => {c.registerView('grid', () => null); if (broken) throw new Error('fixture failure');})]);
  t.after(() => host.dispose());
  await host.reconcile([catalog(m)]);
  assert.equal(host.getSnapshot().views.length, 0); assert.equal(host.getSnapshot().plugins[0].state, 'failed');
  broken = false; await host.reconcile([catalog(m)]);
  assert.equal(host.getSnapshot().views.length, 1); assert.equal(host.getSnapshot().plugins[0].state, 'active');
});

test('only one renderer owner is mounted and switching disposes old effects before activation', async t => {
  const a = manifest('example.a'), b = manifest('example.b');
  const events = [], owners = new Set();
  const tracked = m => definition(m, c => {
    assert.equal(owners.size, 0, 'another renderer still owns live effects');
    owners.add(key(m)); events.push('activate:' + m.plugin_id);
    c.effect(() => () => {owners.delete(key(m)); events.push('dispose:' + m.plugin_id);});
    c.registerView('grid', () => m.plugin_id);
  });
  const host = new BrowserPluginHost([tracked(a), tracked(b)]); t.after(() => host.dispose());
  host.subscribe(() => assert.ok(new Set(host.getSnapshot().views.map(v => v.owner)).size <= 1));
  await host.reconcile([catalog(a), catalog(b)]);
  assert.deepEqual(host.getSnapshot().plugins.map(p => p.state), ['active', 'standby']);
  assert.ok(host.getSnapshot().views.every(v => v.owner === key(a)));
  await host.selectRenderer(key(b));
  assert.deepEqual(events, ['activate:example.a', 'dispose:example.a', 'activate:example.b']);
  assert.equal(host.getSnapshot().selectedRenderer, key(b));
  assert.deepEqual(host.getSnapshot().plugins.map(p => p.state), ['standby', 'active']);
  assert.ok(host.getSnapshot().views.every(v => v.owner === key(b)));
  await host.selectRenderer(null); assert.equal(owners.size, 0); assert.equal(host.getSnapshot().views.length, 0);
  await host.reconcile([catalog(a), catalog(b)]); assert.equal(host.getSnapshot().views.length, 0, 'explicit opt-out must survive refresh');
});

test('dependencies activate in reverse order, withdraw, and recover with their provider', async t => {
  const consumer = manifest('example.consumer'), provider = serviceManifest('example.provider');
  const user = {...definition(consumer, c => c.registerView('grid', () => c.get('fixtureValue'))), requires: ['fixtureValue']};
  const producer = definition(provider, c => c.provide('fixtureValue', 'available'));
  const host = new BrowserPluginHost([user, producer]); t.after(() => host.dispose());
  await host.reconcile([catalog(consumer)]); assert.equal(host.getSnapshot().plugins[0].state, 'waiting');
  await host.reconcile([catalog(consumer), catalog(provider)]);
  assert.equal(host.getSnapshot().views[0].component({}), 'available');
  await host.reconcile([catalog(consumer)]);
  assert.equal(host.getSnapshot().views.length, 0); assert.equal(host.getSnapshot().plugins[0].state, 'waiting');
  await host.reconcile([catalog(consumer), catalog(provider)]); assert.equal(host.getSnapshot().views.length, 1);
});

test('a failed delayed consumer releases its effects', async t => {
  const consumer = manifest('example.consumer'), provider = serviceManifest('example.provider');
  const user = {...definition(consumer, c => {c.registerView('grid', () => null); throw new Error('late failure');}), requires: ['fixtureValue']};
  const host = new BrowserPluginHost([user, definition(provider, c => c.provide('fixtureValue', true))]); t.after(() => host.dispose());
  await host.reconcile([catalog(consumer), catalog(provider)]);
  assert.equal(host.getSnapshot().views.length, 0);
  assert.equal(host.getSnapshot().plugins.find(p => p.key === key(consumer)).state, 'failed');
});

test('uninstalled or incompatible renderer falls back while dependencies remain active', async t => {
  const a = manifest('example.a'), b = manifest('example.b'), provider = serviceManifest('example.provider');
  let serviceDisposals = 0;
  const host = new BrowserPluginHost([definition(a), definition(b), definition(provider, c => {
    c.effect(() => () => {serviceDisposals++;}); c.provide('fixtureValue', true);
  })], key(b)); t.after(() => host.dispose());
  await host.reconcile([catalog(a), catalog(b), catalog(provider)]);
  assert.equal(host.getSnapshot().selectedRenderer, key(b));
  await host.reconcile([catalog(a), catalog(provider)]);
  assert.equal(host.getSnapshot().selectedRenderer, key(a));
  assert.equal(serviceDisposals, 0);
  assert.equal(host.getSnapshot().plugins.find(p => p.key === key(provider)).state, 'active');
  await assert.rejects(host.selectRenderer(key(b)), /不可用/);
  assert.equal(host.getSnapshot().selectedRenderer, key(a));
});

test('service-only plugins cannot claim a renderer slot', async t => {
  const provider = serviceManifest('example.provider');
  const host = new BrowserPluginHost([definition(provider, c => c.registerView('grid', () => null))]);
  t.after(() => host.dispose()); await host.reconcile([catalog(provider)]);
  assert.equal(host.getSnapshot().plugins[0].state, 'failed');
  assert.equal(host.getSnapshot().views.length, 0);
});

test('renderer switch waits for asynchronous cleanup before mounting a replacement', async t => {
  const a=manifest('example.a'),b=manifest('example.b');
  let release,cleanupStarted,activated=false;
  const barrier=new Promise(resolve=>{release=resolve;}),started=new Promise(resolve=>{cleanupStarted=resolve;});
  const host=new BrowserPluginHost([
    definition(a,c=>{c.registerView('grid',()=>null);c.effect(()=>async()=>{cleanupStarted();await barrier;});}),
    definition(b,c=>{activated=true;c.registerView('grid',()=>null);}),
  ]);t.after(()=>host.dispose());
  await host.reconcile([catalog(a),catalog(b)]);
  const switching=host.selectRenderer(key(b));await started;
  assert.equal(activated,false);assert.equal(host.getSnapshot().views.length,0);
  release();await switching;
  assert.equal(activated,true);assert.equal(host.getSnapshot().selectedRenderer,key(b));
});

test('new catalog wins queued reconciliation; disposed hosts cannot resurrect', async () => {
  const m = manifest(), host = new BrowserPluginHost([definition(m)]);
  await Promise.all([host.reconcile([catalog(m)]), host.reconcile([])]);
  assert.equal(host.getSnapshot().views.length, 0);
  const loading = host.reconcile([catalog(m)]);
  await host.dispose(); await loading;
  await host.reconcile([catalog(m)]); assert.equal(host.getSnapshot().views.length, 0);
});
