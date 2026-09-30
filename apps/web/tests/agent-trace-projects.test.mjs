import test from 'node:test';
import assert from 'node:assert/strict';
import { agentTraceGroupId, agentTraceProjects, mergeProjectTraces, traceProjectOptions, traceProjectSelection, withAgentTraceLabels } from '../src/lib/agent-trace-projects.ts';

const projects = [
  { project_id: 'benchmark-a', name: 'Benchmark A', created_at: '2026-01-01' },
  { project_id: 'agent-00000000000000000001', name: 'Agent Trace · guest-one', created_at: '2026-01-02' },
  { project_id: 'agent-00000000000000000002', name: 'Agent Trace · guest-two', created_at: '2026-01-03' },
  { project_id: 'agent-not-a-guest', name: 'Other project', created_at: '2026-01-04' },
];

test('guest Agent Trace projects appear as one selectable collection', () => {
  assert.deepEqual(agentTraceProjects(projects).map(item => item.project_id), [projects[1].project_id, projects[2].project_id]);
  assert.deepEqual(traceProjectOptions(projects).map(item => item.value), ['benchmark-a', 'agent-not-a-guest', agentTraceGroupId]);
  assert.equal(traceProjectSelection(projects, projects[1].project_id), agentTraceGroupId);
  assert.equal(traceProjectSelection(projects, 'benchmark-a'), 'benchmark-a');
});

test('merged rows keep their source project for direct trace links', () => {
  const rows = mergeProjectTraces([
    { project_id: projects[1].project_id, items: [{ run_id: 'older', revision: 1, created_at: '2026-01-01T00:00:00Z' }] },
    { project_id: projects[2].project_id, items: [{ run_id: 'newer', revision: 1, created_at: '2026-01-02T00:00:00Z' }] },
  ]);
  assert.deepEqual(rows.map(item => [item.run_id, item.project_id]), [
    ['newer', projects[2].project_id], ['older', projects[1].project_id],
  ]);
});

test('query label is attached only to the matching trace project and run', () => {
  const rows = [
    { project_id: projects[1].project_id, run_id: 'agent-turn', revision: 1 },
    { project_id: projects[2].project_id, run_id: 'agent-turn', revision: 1 },
  ];
  const labeled = withAgentTraceLabels(rows, [
    { project_id: projects[1].project_id, run_id: 'agent-turn', title: 'Find tool failure' },
  ]);
  assert.equal(labeled[0].display_title, 'Find tool failure');
  assert.equal(labeled[1].display_title, undefined);
});
