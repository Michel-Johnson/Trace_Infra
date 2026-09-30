import test from 'node:test';
import assert from 'node:assert/strict';
import { taskAgentTarget, taskAgentHref, linkTasksToAgentSessions } from '../src/lib/task-agent-link.ts';

const sessionId = '11111111-1111-4111-8111-111111111111';
const task = (source, kind = 'evaluation') => ({
  task_id: 'task-1', project_id: 'benchmark-a', kind, title: '评测', state: 'running',
  current_stage: 'verify', progress: 0.9, processed: 10, total: 10,
  created_at: '2026-09-29T00:00:00Z', started_at: '2026-09-29T00:00:00Z',
  updated_at: '2026-09-29T00:00:01Z', completed_at: null, revision: 2,
  steps: [], events: [], artifacts: [], result: null, error: null, source,
});

test('agent task deep link preserves task identity and targets an existing interactive terminal', () => {
  const target = taskAgentTarget(task({ type: 'agent', agent_session_id: sessionId,
    agent_session_kind: 'interactive' }));
  assert.deepEqual(target, { sessionId, kind: 'interactive' });
  const href = taskAgentHref('/tasks', '?project=benchmark-a&task=task-1', target);
  const params = new URLSearchParams(href.search);
  assert.equal(params.get('task'), 'task-1');
  assert.equal(params.get('agent_session'), sessionId);
  assert.equal(params.get('agent_session_kind'), 'interactive');
});

test('print-mode worker task has no CUI link, even when a journal identity exists', () => {
  const linked = taskAgentTarget(task({ type: 'agent', agent_session_id: sessionId,
    agent_session_kind: 'background' }));
  assert.equal(linked, null);
  assert.equal(taskAgentTarget(task({ type: 'agent' })), null);
  assert.equal(taskAgentTarget(task({ type: 'agent', agent_session_id: '../other',
    agent_session_kind: 'interactive' })), null);
});

test('unrelated import tasks have no inferred Agent target', () => {
  assert.equal(taskAgentTarget(task({ type: 'auto_import', batch_id: 'batch-1' }, 'adapter_import')), null);
});

test('existing owner-scoped Agent session list fills links without changing unrelated tasks', () => {
  const agent = task({ type: 'agent' });
  const otherProject = { ...agent, project_id: 'benchmark-b' };
  const manual = task({ type: 'cli' });
  const linked = linkTasksToAgentSessions([agent, otherProject, manual], [{
    session_id: sessionId, task_id: 'task-1', project_id: 'benchmark-a',
    native_terminal: false, claude_session_id: null,
  }]);
  assert.equal(taskAgentTarget(linked[0]), null);
  assert.equal(taskAgentTarget(linked[1]), null);
  assert.equal(linked[2], manual);
  assert.equal(agent.source.agent_session_id, undefined);
});

test('native import session only resumes when confirmed by server', () => {
  const agent = task({ type: 'auto_import' }, 'adapter_import');
  const linked = linkTasksToAgentSessions([agent], [{
    session_id: sessionId, task_id: 'task-1', project_id: 'benchmark-a',
    native_terminal: true, claude_session_id: sessionId,
  }]);
  assert.equal(taskAgentTarget(linked[0])?.kind, 'native_import');
});
