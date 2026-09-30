import test from 'node:test';
import assert from 'node:assert/strict';
import { createTerminalTab, openTerminalTab } from '../src/lib/terminal-tabs.ts';

test('new session adds a tab without replacing or remounting the prior terminal', () => {
  const first = createTerminalTab();
  const initial = { items: [first], activeId: first.id };
  const next = openTerminalTab(initial);
  assert.equal(next.items.length, 2);
  assert.strictEqual(next.items[0], first);
  assert.equal(next.activeId, next.items[1].id);
  assert.notEqual(next.items[1].target.nonce, first.target.nonce);
  assert.equal(openTerminalTab(next).items.length, 3);
});

test('restoring an already-open session activates its existing tab', () => {
  const first = createTerminalTab('abc');
  const second = createTerminalTab();
  const current = { items: [first, second], activeId: second.id };
  const next = openTerminalTab(current, 'abc');
  assert.equal(next.items.length, 2);
  assert.strictEqual(next.items[0], first);
  assert.equal(next.activeId, first.id);
});

test('import session identity is separate from an interactive session', () => {
  const first = createTerminalTab('abc');
  const withImport = openTerminalTab({ items: [first], activeId: first.id }, null, 'abc');
  assert.equal(withImport.items.length, 2);
  assert.equal(openTerminalTab(withImport, null, 'abc').items.length, 2);
});
