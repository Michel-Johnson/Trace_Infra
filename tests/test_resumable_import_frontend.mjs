import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';

const client = await readFile(new URL('../apps/web/src/api/infra-client.ts', import.meta.url), 'utf8');
const page = await readFile(new URL('../apps/web/src/components/InfraPages.tsx', import.meta.url), 'utf8');
const terminal = await readFile(new URL('../apps/web/src/components/AgentTerminal.tsx', import.meta.url), 'utf8');
const shell = await readFile(new URL('../apps/web/src/components/AppShell.tsx', import.meta.url), 'utf8');
const generated = await readFile(new URL('../apps/web/src/api/generated.ts', import.meta.url), 'utf8');
const styles = await readFile(new URL('../apps/web/src/workspace.css', import.meta.url), 'utf8');

test('Trace uploads start an Agent task and recover task state after refresh', () => {
  for (const method of ['agentCapabilities','createUpload','uploadStatus','uploadPart','completeUpload',
    'taskStatus','retryTask','cancelTask']) assert.match(client, new RegExp(`${method}:`));
  for (const path of ['imports/uploads','missing_parts','X-Chunk-SHA256','agent_session_id'])
    assert.match(`${client}\n${generated}`, new RegExp(path));
  assert.match(page, /source_format: 'auto'/);
  assert.match(page, /infraApi\.retryTask\(project, item\.taskId\)/);
  assert.match(page, /infraApi\.cancelTask\(project, item\.taskId\)/);
  assert.doesNotMatch(client, /retryUpload:|cancelUpload:|createImport:|importJob:/);
  assert.match(page, /await infraApi\.agentCapabilities/);
  assert.match(page, /await infraApi\.taskStatus/);
  assert.match(page, /trace-hunter-auto-import:/);
  assert.match(page, /importRequestId\(\)/);
  assert.match(page, /上传\{item\.upload \? ' · 可断点续传' : ''\}/);
  for (const stage of ['format_inspect','adapter_select','adapter_validate','trace_import','readback'])
    assert.match(page, new RegExp(stage));
});

test('auto-import exposes the persisted Agent session and distinguishes upload from readback', () => {
  assert.match(shell, /initialImportSessionId=\{importSessionId\}/);
  assert.match(page, /agent_session/);
  assert.match(page, /className="th-import-actions"/);
  const fileHeader = page.indexOf('<header><div><strong>{item.fileName}</strong>');
  const actionRow = page.indexOf('className="th-import-actions"', fileHeader);
  assert.ok(fileHeader >= 0 && actionRow > fileHeader && actionRow < page.indexOf('</header>', fileHeader));
  assert.match(page, /className="th-import-action"[^>]*>打开会话<\/Link>/);
  assert.match(page, /className="th-import-action"[^>]*>\{item\.state === 'duplicate' \? '查看已导入的 Trace' : '查看导入的 Trace'\}<\/Link>/);
  assert.match(page, />打开会话<\/Link>/);
  assert.match(page, /item\.uploadId \? infraApi\.uploadStatus\(project, item\.uploadId\)/);
  assert.match(terminal, /unifyAgentSessions/);
  assert.match(terminal, /params\.set\('resume', importSessionId\)/);
  assert.match(terminal, /\.trace-hunter-imports\/\$\{importSessionId\}/);
  assert.doesNotMatch(terminal, /params\.set\('import_session'/);
  assert.match(terminal, /<iframe ref=\{iframe\}/);
  assert.doesNotMatch(terminal, /AgentSessionView/);
  assert.match(page, /尚未导入成功/);
  assert.match(page, /只有回读完成才算导入成功/);
  assert.doesNotMatch(page, /查看(?:本次|已有) Agent Trace/);
  assert.match(page, /active \|\| items\.some\(item => item\.state === 'queued' \|\| item\.state === 'running'\)/);
});

test('successful import status and imported Trace link are not styled as errors', () => {
  assert.match(page, /className="th-import-error" role="alert"/);
  assert.match(styles, /\.th-batch-files>article>p\{padding:0;border-radius:0;background:transparent/);
  assert.match(styles, /\.th-batch-files>article\.failed>p\.th-import-current-state,\.th-batch-files>article>p\.th-import-error\{[^}]*background:#fff0ed/);
});
