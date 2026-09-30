import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';

const client = await readFile(new URL('../apps/web/src/api/infra-client.ts', import.meta.url), 'utf8');
const page = await readFile(new URL('../apps/web/src/components/InfraPages.tsx', import.meta.url), 'utf8');
const contract = await readFile(new URL('../contracts/openapi.json', import.meta.url), 'utf8');

test('Trace Infra query page exposes only real OpenAPI-backed operations', () => {
  for (const method of ['objects','spanWindow','metrics','evidenceExport']) {
    assert.match(client, new RegExp(`${method}:`));
    assert.match(page, new RegExp(`infraApi\.${method}`));
  }
  for (const label of ['对象证据','前后步骤','指标统计','批量证据导出']) assert.match(page, new RegExp(label));
  assert.equal(JSON.parse(contract).info.version, "4.0.0");
  assert.doesNotMatch(page, /即将开放|敬请期待/);
});

test('task client reserves cursor filters and uses backend import batch totals', () => {
  assert.match(client, /TaskListQuery/);
  assert.match(client, /query\.after/);
  assert.match(client, /next_cursor: string \| null/);
  assert.match(client, /retention_limit: number/);
  assert.match(client, /importBatch:/);
  assert.match(client, /query: \{ limit\?: number; after\?: string \| null \}/);
  assert.match(page, /infraApi\.importBatch/);
  assert.match(page, /batch\?\.total/);
});
