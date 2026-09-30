import test from 'node:test';
import assert from 'node:assert/strict';
import { unifyAgentSessions } from '../src/lib/agent-session-catalog.ts';

test('interactive and import sessions share one ordered catalog without mixing identities', () => {
  const rows = unifyAgentSessions([
    { id: '11111111-1111-4111-8111-111111111111', title: '查 Trace', mtime: 2, attached: true },
  ], [
    { session_id: '22222222-2222-4222-8222-222222222222', project_id: 'synthetic',
      claude_session_id: '22222222-2222-4222-8222-222222222222',
      native_terminal: true,
      kind: 'auto_import', source_name: 'case.json', state: 'running', task_id: 'task-synthetic',
      updated_at: '1970-01-01T00:00:03Z' },
  ]);
  assert.deepEqual(rows.map(item => item.kind), ['auto_import', 'interactive']);
  assert.equal(rows[0].label, '导入 · case.json');
  assert.equal(rows[0].taskId, 'task-synthetic');
  assert.equal(rows[1].sessionId, '11111111-1111-4111-8111-111111111111');
  assert.notEqual(rows[0].key, rows[1].key);
});

test('legacy print-mode sessions are omitted from the CUI history', () => {
  const rows = unifyAgentSessions([], [{
    session_id: '33333333-3333-4333-8333-333333333333', project_id: 'synthetic',
    claude_session_id: '33333333-3333-4333-8333-333333333333', native_terminal: false,
    kind: 'auto_import', source_name: 'legacy.json',
    state: 'succeeded', task_id: 'task-legacy', updated_at: '1970-01-01T00:00:03Z',
  }]);
  assert.deepEqual(rows, []);
});

test('native Claude session is offered before Claude creates its task', () => {
  const sessionId = '44444444-4444-4444-8444-444444444444';
  const rows = unifyAgentSessions([], [{ session_id: sessionId, project_id: 'synthetic',
    claude_session_id: sessionId, native_terminal: true, kind: 'agent', source_name: null,
    state: 'running', task_id: null, updated_at: '1970-01-01T00:00:03Z' }]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].sessionId, sessionId);
  assert.equal(rows[0].label, 'Claude · synthetic');
});

test('invalid IDs are not offered for restoring a terminal', () => {
  assert.deepEqual(unifyAgentSessions([{ id: '../private', title: 'bad', mtime: 0, attached: false }], []), []);
});
