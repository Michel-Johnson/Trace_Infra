import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import ts from '../apps/web/node_modules/typescript/lib/typescript.js';

const source = await readFile(new URL('../apps/web/src/lib/trace-timeline-range.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022}}).outputText;
const {clampTraceRange, intersectTraceRange, moveTraceRange, resizeTraceRange, traceSpanIntersectsRange, traceTimelineUsesDuration} = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);

test('a pinned trace range moves without changing width and stops at both bounds', () => {
  assert.deepEqual(moveTraceRange({start: 20, end: 40}, 15, {start: 0, end: 100}), {start: 35, end: 55});
  assert.deepEqual(moveTraceRange({start: 20, end: 40}, -50, {start: 0, end: 100}), {start: 0, end: 20});
  assert.deepEqual(moveTraceRange({start: 70, end: 90}, 50, {start: 0, end: 100}), {start: 80, end: 100});
});

test('range handles preserve the opposite edge and enforce a minimum interval', () => {
  assert.deepEqual(resizeTraceRange({start: 20, end: 60}, 'start', 55, {start: 0, end: 100}, 10), {start: 50, end: 60});
  assert.deepEqual(resizeTraceRange({start: 20, end: 60}, 'end', 22, {start: 0, end: 100}, 10), {start: 20, end: 30});
  assert.deepEqual(resizeTraceRange({start: 20, end: 60}, 'end', 120, {start: 0, end: 100}, 10), {start: 20, end: 100});
});

test('range visibility is clipped to the current zoom domain without mutating the pinned range', () => {
  assert.deepEqual(intersectTraceRange({start: 10, end: 70}, {start: 30, end: 50}), {start: 30, end: 50});
  assert.equal(intersectTraceRange({start: 10, end: 20}, {start: 30, end: 50}), null);
  assert.deepEqual(clampTraceRange({start: 80, end: -10}, {start: 0, end: 100}), {start: 0, end: 80});
});

test('an untimed trace uses ordinal coordinates for both drawing and filtering', () => {
  const spans = Array.from({length: 6}, () => ({start_ms: null, end_ms: null}));
  assert.equal(traceTimelineUsesDuration(spans, true), false);
  assert.deepEqual(spans.map((span, index) => traceSpanIntersectsRange(span, index, {start: 2.1, end: 4.8}, false)), [false, false, true, true, true, false]);
});

test('duration coordinates are used only when every span has complete timing', () => {
  const complete = [{start_ms: 10, end_ms: 20}, {start_ms: 30, end_ms: 40}];
  assert.equal(traceTimelineUsesDuration(complete, true), true);
  assert.equal(traceSpanIntersectsRange(complete[1], 1, {start: 35, end: 50}, true), true);
  assert.equal(traceSpanIntersectsRange(complete[0], 0, {start: 20, end: 30}, true), false);
  assert.equal(traceTimelineUsesDuration([{start_ms: 10, end_ms: 20}, {start_ms: null, end_ms: null}], true), false);
});
