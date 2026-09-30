import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const css = await readFile(new URL('../apps/web/src/workspace.css', import.meta.url), 'utf8');
const source = await readFile(new URL('../apps/web/src/components/InfraPages.tsx', import.meta.url), 'utf8');

test('DSH trace view keeps its structural layout rules', () => {
  assert.match(css, /\.th-dsh-trace-head\{[^}]*display:flex[^}]*justify-content:space-between/);
  assert.match(css, /\.th-dsh-lanes\{display:grid/);
  assert.match(css, /\.th-dsh-lane\{display:grid/);
  assert.match(css, /\.th-dsh-lane>i\{position:relative/);
  assert.match(css, /\.th-dsh-lane>i>button\{position:absolute/);
  assert.match(css, /\.th-dsh-events::before\{position:absolute[^}]*content:""/);
  assert.match(css, /\.th-dsh-event-copy\{min-width:0/);
  assert.doesNotMatch(css, /th-trace-splitter|th-trace-inspector-resize/);
  assert.match(css, /\.th-dsh-trace-body\{[^}]*height:auto[^}]*overflow:visible/);
  assert.match(css, /\.th-case-dsh \.th-dsh-trace-body\{[^}]*height:auto/);
  assert.match(css, /\.th-dsh-events\{[^}]*overflow:visible/);
  assert.match(css, /\.th-trace-inspector-body\{[^}]*overflow:visible/);
  assert.doesNotMatch(source, /role="separator"|inspectorWidth|resizeWithKeyboard|panStart|traceBodyRef/);
  assert.match(source, /target\.scrollIntoView\(\{ block: 'nearest', behavior: 'auto' \}\)/);
});
