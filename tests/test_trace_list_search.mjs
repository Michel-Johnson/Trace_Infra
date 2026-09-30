import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('../apps/web/src/components/InfraPages.tsx', import.meta.url), 'utf8');
const css = await readFile(new URL('../apps/web/src/workspace.css', import.meta.url), 'utf8');

test('trace list supports immediate ID filtering', () => {
  assert.match(source, /placeholder="搜索 Trace ID" aria-label="搜索 Trace ID"/);
  assert.match(source, /\[item\.run_id, item\.query_id\]\.some\(value => String\(value \|\| ''\)\.toLocaleLowerCase\(\)\.includes\(query\)\)/);
  assert.match(source, /visibleTraces\.map\(item =>/);
  assert.match(source, /没有匹配的 Trace ID/);
  assert.match(css, /\.th-infra-toolbar \.th-trace-id-search\{[^}]*width:min\(340px,32vw\)[^}]*height:38px/);
  assert.match(css, /\.th-trace-id-search:focus-within\{[^}]*border-color:#6f82db/);
});
