import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('../apps/web/src/components/InfraPages.tsx', import.meta.url), 'utf8');
const css = readFileSync(new URL('../apps/web/src/workspace.css', import.meta.url), 'utf8');
const inspector = source.slice(source.indexOf('function TraceSpanInspector('), source.indexOf("type TraceRangeDragMode ="));

test('DSH inspector tabs follow the span role', () => {
  assert.match(source, /tools: \[\['summary', 'Summary'\], \['payload', 'Payload'\], \['result', 'Result'\], \['schema', 'Schema'\], \['timing', 'Timing'\]\]/);
  assert.match(source, /model: \[\['summary', 'Summary'\], \['preview', 'Preview'\], \['raw', 'Raw'\]\]/);
  assert.match(source, /input: \[\['summary', 'Summary'\], \['preview', 'Preview'\], \['raw', 'Raw'\], \['source', 'Source'\]\]/);
  assert.match(inspector, /const tabs = inspectorTabsByLane\[lane\]/);
  assert.match(inspector, /parentSpan\?: SpanItem/);
  assert.match(source, /parentSpan=\{spans\.find\(item => item\.span_id === activeSpan\.parent_id\)\}/);
});

test('summary presents source, status, tokens, preview and timing without losing metadata', () => {
  for (const label of ['Source', 'Status', 'Tokens', 'Preview', 'Request Timing', 'Parent Span', 'Context']) {
    assert.ok(inspector.includes(label), `${label} should remain available`);
  }
  assert.match(inspector, /<details className="th-trace-inspector-section" key=\{.*?:preview.*?\} open><summary>Preview<\/summary>/);
  assert.match(inspector, /<details className="th-trace-inspector-section" key=\{.*?:timing.*?\} open><summary>Request Timing<\/summary>/);
});

test('Start offset uses seconds while duration keeps its own formatter', () => {
  assert.match(inspector, /\['Start offset', `\$\{\(span\.start_ms \/ 1000\)\.toFixed\(1\)\} s`\]/);
  assert.doesNotMatch(inspector, /\['Start offset', `\$\{span\.start_ms\.toFixed\(1\)\} ms`\]/);
  assert.match(inspector, /\['Total duration', compactDuration\(span\.duration_ms\)\]/);
  assert.equal((152911.7 / 1000).toFixed(1), '152.9');
});

test('tool fields have distinct tabs and model requests remain lazy', () => {
  assert.match(inspector, /tab === 'payload' && .*?<TraceValue value=\{input\}/);
  assert.match(inspector, /tab === 'result' && .*?<TraceValue value=\{output\}/);
  assert.match(inspector, /tab === 'schema' && .*?<TraceValue value=\{schema\}/);
  assert.match(inspector, /tab === 'timing' && .*?timingRows\.map/);
  assert.match(inspector, /tab !== 'preview' \|\| !requestExpanded \|\| !onLoadSource \|\| !requestId/);
  assert.match(inspector, /onToggle=\{event => setRequestExpanded\(event.currentTarget.open\)\}/);
  assert.match(inspector, /sourceRefs\.map\(\(ref, sourceIndex\) =>/);
  assert.match(inspector, /tab === 'raw' && <TraceValue value=\{raw \?\? span\}\/>/);
});

test('user summary includes source and duration while tool summary links its parent', () => {
  assert.match(inspector, /lane === 'input' \? 'User'/);
  assert.match(inspector, /lane === 'input' && span\.duration_ms != null/);
  assert.match(inspector, /lane === 'input' \? input \?\? output/);
  assert.match(inspector, /lane === 'tools' \? <div><dt>Hierarchy<\/dt>/);
  assert.match(inspector, /tab === 'source' &&/);
});

test('model summaries use captured response text and token usage without fabricating missing timing', () => {
  assert.match(source, /function modelResponseSummary\(value: unknown\)/);
  assert.match(inspector, /onLoadSource\(responseId\)\.then\(value =>/);
  assert.match(inspector, /lane === 'model' && responseSummary\.text \? responseSummary\.text/);
  assert.match(inspector, /usageRows\.push\(\['Tokens', `\$\{outputTokens\.toLocaleString\(\)\} tok`\]\)/);
  assert.match(inspector, /primaryTiming\[label\]/);
  assert.match(source, /rawTurns=\{rawTrace\?\.turns\}/);
  assert.match(inspector, /turnNumber && stepNumber \? `Turn \$\{turnNumber\} · Step \$\{stepNumber\}`/);
});

test('inspector styling remains scoped and readable', () => {
  assert.match(css, /\.th-trace-inspector>nav button\{[^}]*font-size:14px/);
  assert.match(css, /\.th-trace-inspector-body dt,\.th-trace-inspector-body dd\{font-size:14px/);
  assert.match(css, /@media\(max-width:980px\)\{\.th-trace-inspector\{width:100%;flex:0 0 auto\}\}/);
});

test('long trace scrolls events and inspector details independently on desktop', () => {
  assert.match(css, /\.th-dsh-trace-body\{[^}]*height:max\(420px,calc\(100dvh - 300px\)\)[^}]*overflow:hidden/);
  assert.match(css, /\.th-dsh-events\{[^}]*overflow-y:auto;overscroll-behavior:auto/);
  assert.match(css, /\.th-trace-inspector-body\{[^}]*overflow-y:auto;overscroll-behavior:auto/);
  assert.match(css, /@media\(max-width:980px\)\{\.th-dsh-trace-body\{[^}]*overflow:visible/);
});

test('Trace detail consumes the outer bottom padding and grows to the viewport', () => {
  assert.match(css, /@media\(min-width:981px\)\{\.th-section-traces:has\(\.th-dsh-trace\)\{padding-bottom:0/);
  assert.match(css, /\.th-section-traces \.th-infra-page:has\(> \.th-dsh-trace\)\{padding-bottom:0/);
  assert.match(css, /\.th-section-traces \.th-dsh-trace-body\{height:max\(420px,calc\(100dvh - 148px\)\)/);
});

test('role-specific tabs remain keyboard reachable', () => {
  assert.match(inspector, /onKeyDown=\{event => \{/);
  assert.match(inspector, /event\.key === 'ArrowRight'/);
  assert.match(inspector, /event\.key === 'ArrowLeft'/);
  assert.match(inspector, /event\.currentTarget\.parentElement\?\.querySelectorAll<HTMLButtonElement>/);
});
