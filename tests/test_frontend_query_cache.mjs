import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import ts from '../apps/web/node_modules/typescript/lib/typescript.js';

const source = await readFile(new URL('../apps/web/src/api/query-cache.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const cache = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'));
const workspace = await readFile(new URL('../apps/web/src/components/Workspace.tsx', import.meta.url), 'utf8');
const pages = await readFile(new URL('../apps/web/src/components/InfraPages.tsx', import.meta.url), 'utf8');

test('query cache deduplicates requests and reuses fresh values', async () => {
  cache.clearQueryCache();
  let calls = 0;
  const loader = async () => { calls += 1; return ['trace-1']; };
  const key = cache.cacheKey('trace-list', 'project-a');
  const [first, second] = await Promise.all([
    cache.loadCachedQuery(key, loader, { ttlMs: 30_000 }),
    cache.loadCachedQuery(key, loader, { ttlMs: 30_000 }),
  ]);
  assert.equal(calls, 1);
  assert.deepEqual(first, ['trace-1']);
  assert.equal(second, first);
  assert.equal(await cache.loadCachedQuery(key, loader, { ttlMs: 30_000 }), first);
  assert.equal(calls, 1);
});

test('stale data remains readable while a forced refresh is pending', async () => {
  cache.clearQueryCache();
  const key = cache.cacheKey('observability', 'project-a');
  cache.setCachedQuery(key, { traces: 10 });
  let resolve;
  const pending = cache.loadCachedQuery(key, () => new Promise(done => { resolve = done; }), { ttlMs: 30_000, force: true });
  assert.deepEqual(cache.peekCachedQuery(key), { traces: 10 });
  resolve({ traces: 11 });
  await pending;
  assert.deepEqual(cache.peekCachedQuery(key), { traces: 11 });
});

test('project invalidation prevents an older in-flight query from restoring stale data', async () => {
  cache.clearQueryCache();
  const key = cache.cacheKey('trace-list', 'project-a');
  let resolve;
  const pending = cache.loadCachedQuery(key, () => new Promise(done => { resolve = done; }), { ttlMs: 30_000 });
  cache.invalidateCachedQueries(cache.cacheKeyPrefix('trace-list', 'project-a'));
  resolve(['stale-trace']);
  await pending;
  assert.equal(cache.peekCachedQuery(key), undefined);
});

test('cache evicts least recently used completed entries', () => {
  cache.clearQueryCache();
  for (let index = 0; index < 70; index += 1) cache.setCachedQuery(cache.cacheKey('entry', index), index);
  assert.equal(cache.peekCachedQuery(cache.cacheKey('entry', 0)), undefined);
  assert.equal(cache.peekCachedQuery(cache.cacheKey('entry', 69)), 69);
});

test('route pages render cached data before background refresh and imports invalidate project data', () => {
  assert.match(workspace, /infraApi\.tasks\(project, \{ limit: 200 \}/);
  assert.match(workspace, /const \[loading, setLoading\] = useState\(!demoMode\)/);
  assert.match(pages, /cacheKey\('trace-content', project, runId, selectedRevision\)/);
  assert.match(pages, /busy && !traces\.length \? <InfraBusy\/>/);
  assert.match(pages, /invalidateProjectCache\(project\)/);
  assert.doesNotMatch(pages, /window\.location\.reload/);
});
