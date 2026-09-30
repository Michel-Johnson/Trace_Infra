import assert from 'node:assert/strict';
import test from 'node:test';
import ts from '../apps/web/node_modules/typescript/lib/typescript.js';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../apps/web/src/lib/task-groups.ts', import.meta.url), 'utf8');
const pageSource = await readFile(new URL('../apps/web/src/components/InfraPages.tsx', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { groupInfraTasks } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);

const task = (id, batch, state = 'succeeded', progress = 1) => ({
  task_id: id, project_id: 'p', kind: 'adapter_import', title: `${id}.json`, state, current_stage: state === 'running' ? '转换中' : null,
  progress, processed: Math.round(progress * 6), total: 6, created_at: '2026-09-22T00:00:00Z', started_at: null,
  updated_at: `2026-09-22T00:00:0${id.length}Z`, completed_at: null, revision: 1, steps: [], events: [],
  artifacts: state === 'succeeded' ? [{ kind: 'trace', name: id }] : [], result: null, error: null,
  source: batch ? { batch_id: batch, type: 'adapter_import' } : {},
});

test('groups import files by batch and keeps coarse progress facts', () => {
  const [group] = groupInfraTasks([task('a', 'batch-1'), task('bb', 'batch-1', 'running', .5)]);
  assert.equal(group.title, 'batch-1');
  assert.equal(group.itemCount, 2);
  assert.equal(group.completedCount, 1);
  assert.equal(group.state, 'running');
  assert.equal(group.progress, .75);
  assert.deepEqual(group.activeTaskIds, ['bb']);
  assert.equal(group.artifacts.length, 1);
});

test('does not merge unrelated or unbatched tasks', () => {
  const groups = groupInfraTasks([task('a', ''), { ...task('b', ''), kind: 'analysis' }]);
  assert.equal(groups.length, 2);
  assert.ok(groups.every(group => !group.isBatch));
});

test('failed tasks keep their actual failed stage for a task deep link', () => {
  const failed = { ...task('a', '', 'failed', .5), kind: 'custom', current_stage: 'capture' };
  const [group] = groupInfraTasks([failed]);
  assert.equal(group.state, 'failed');
  assert.equal(group.currentStage, 'capture');
  assert.deepEqual(group.taskIds, ['a']);
  assert.match(pageSource, /infraApi\.taskStatus\(project, targetTaskId, ctrl\.signal\)/);
  assert.match(pageSource, /task\.isBatch \? task\.title : task\.taskIds\[0\]/);
  assert.match(pageSource, /未记录具体错误/);
});

test('limited task history is described as recent records instead of a complete batch total', () => {
  assert.match(pageSource, /最近 \$\{task\.itemCount\} 个文件，已完成 \$\{task\.completedCount\}/);
  assert.match(pageSource, /partial \? '最近记录' : '文件'/);
  assert.doesNotMatch(pageSource, /当前仅展示最近 200 个任务/);
});
