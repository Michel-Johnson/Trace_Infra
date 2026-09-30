import test from 'node:test';
import assert from 'node:assert/strict';
import { searchAgentTraceProjects } from '../src/lib/agent-trace-search.ts';

function page(runId, nextCursor, total) {
  return {
    items: [{ run_id: runId, revision: 1, object_kind: 'span', object_id: runId,
      span_id: runId, field: 'content', text_state: 'exact', score: 1, snippet: runId,
      snippet_start: 0, text_length: runId.length, snippet_truncated: false,
      match_ranges: [], source_refs: [] }],
    next_cursor: nextCursor, query_digest: 'digest', projector_version: 'v1',
    effective_pattern: null, candidate_count: total, total_count: total,
    matched_trace_count: total, truncated: false,
  };
}

test('Agent Trace search keeps source project and independent cursors', async () => {
  const calls = [];
  const search = async (project, cursor, limit) => {
    calls.push([project, cursor, limit]);
    if (project === 'a') return cursor ? page('a-2', null, 2) : page('a-1', 'a-next', 2);
    return page('b-1', null, 1);
  };
  const first = await searchAgentTraceProjects(['a', 'b'], null, search);
  assert.deepEqual(first.items.map(item => [item.project_id, item.run_id]), [['a', 'a-1'], ['b', 'b-1']]);
  assert.equal(first.total_count, 3);
  assert.ok(first.next_cursor);
  const second = await searchAgentTraceProjects(['a', 'b'], first.next_cursor, search);
  assert.deepEqual(second.items.map(item => [item.project_id, item.run_id]), [['a', 'a-2']]);
  assert.equal(second.total_count, 3);
  assert.equal(second.next_cursor, null);
  assert.deepEqual(calls.map(([project, cursor]) => [project, cursor]),
    [['a', null], ['b', null], ['a', 'a-next']]);
});
