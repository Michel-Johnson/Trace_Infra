import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('../apps/web/src/components/Workspace.tsx', import.meta.url), 'utf8');
const css = readFileSync(new URL('../apps/web/src/components/Workbench.css', import.meta.url), 'utf8');
const shell = readFileSync(new URL('../apps/web/src/components/AppShell.tsx', import.meta.url), 'utf8');
const packageJson = JSON.parse(readFileSync(new URL('../apps/web/package.json', import.meta.url), 'utf8'));
const packageLock = JSON.parse(readFileSync(new URL('../apps/web/package-lock.json', import.meta.url), 'utf8'));

test('workbench puts task actions in the topbar and omits percentage progress', () => {
  assert.match(shell, /id="thw-topbar-controls"/);
  assert.match(source, /createPortal\(<div className="thw-topbar-controls"/);
  assert.match(source, /<Link to="\/imports">导入 Trace<\/Link><Link to="\/infra">运行分析<\/Link>/);
  assert.doesNotMatch(source, /thw-project-row|thw-start-actions|发起新任务/);
  assert.ok(source.indexOf('id="thw-attention-title"') < source.indexOf('id="thw-running-title"'));
  assert.ok(source.indexOf('id="thw-running-title"') < source.indexOf('id="thw-completed-title"'));
  assert.doesNotMatch(source, /role="progressbar"|整体进度|已完成 Trace/);
  assert.match(shell, /section === 'overview' \? '工作台'/);
});

test('workbench uses actual task steps and isolates local demo data', () => {
  assert.match(source, /import\.meta\.env\.MODE === 'mock'/);
  assert.match(source, /infraApi\.tasks\(project, \{ limit: 200 \}/);
  assert.match(source, /task\.steps\.map\(step/);
  assert.match(source, /groupInfraTasks\(tasks\)/);
  assert.match(source, /infraApi\.importBatch\(project, id/);
  assert.match(source, /members\.some\(item => item\.state === 'failed'\)/);
  assert.match(source, /已导入 600 \/ 1,000/);
});

test('stage states and completed task types remain visually distinct', () => {
  assert.match(css, /\.thw-stage-active::before\{[^}]*animation:thw-spin/);
  assert.match(css, /prefers-reduced-motion:reduce/);
  assert.match(css, /\.thw-stage-done::before\{[^}]*var\(--thw-sage\)/);
  assert.match(css, /\.thw-kind-analysis\{background:#eeeafa;color:#5a4a90/);
  assert.match(css, /\.thw-kind-evaluation\{background:#e5f1e8;color:#356448/);
  assert.doesNotMatch(source, /分析 · \d+ 条 Trace|评测 · \d+ 条 Trace/);
});

test('workbench styling does not change the shared sidebar or topbar', () => {
  assert.doesNotMatch(css, /\.th-shell|\.th-sidebar|\.th-navigation|\.th-brand/);
  assert.doesNotMatch(css, /(^|[},])\s*\.th-topbar(?:[ \{,:])/m);
  assert.match(shell, /return <div className="th-shell">/);
});

test('workbench section headings match the topbar serif while task copy stays sans-serif', () => {
  assert.match(css, /\.thw-workbench\{[^}]*font:400 15px\/1\.5 Inter,/);
  assert.match(css, /\.thw-section-head h2\{font-family:var\(--th-serif\);font-size:21px;font-weight:650;letter-spacing:-\.025em;line-height:1\.2\}/);
  assert.match(css, /\.thw-blocked-name\{min-width:0;font-size:17px;font-weight:600/);
  assert.match(css, /\.thw-blocked-issue\{[^}]*font-size:16px;font-weight:600/);
  assert.match(css, /\.thw-stages-horizontal>\.thw-stage>strong\{font-size:15px/);
});

test('attention card leads with its title, then shows an icon and blocker', () => {
  const card = source.slice(source.indexOf('<article className="thw-blocked"'), source.indexOf('</article>)}', source.indexOf('<article className="thw-blocked"')));
  assert.ok(card.indexOf('thw-blocked-name') < card.indexOf('thw-blocked-issue'));
  assert.ok(card.indexOf('thw-blocked-issue') < card.indexOf('查看原因'));
  assert.ok(card.indexOf('查看原因') < card.indexOf('<StageList'));
  assert.match(card, /<svg[^>]*aria-hidden="true"[^>]*><circle[^>]*\/><path d="M12 7v6"\/>/);
  assert.doesNotMatch(card, /task\.impact|thw-blocked-head-actions|thw-blocked-foot/);
  assert.match(css, /\.thw-blocked-head\{display:grid;grid-template-columns:minmax\(0,1fr\) auto auto/);
  assert.match(css, /@media\(max-width:760px\)\{\.thw-blocked-head\{grid-template-columns:minmax\(0,1fr\) auto\}/);
});

test('查看原因 links to the failed task in its project', () => {
  assert.match(source, /taskId: task\.task_id/);
  assert.match(source, /`\/tasks\?project=\$\{encodeURIComponent\(project\)\}&task=\$\{encodeURIComponent\(task\.taskId\)\}`/);
});

test('stage connectors pass through centered markers without horizontal gaps', () => {
  assert.match(css, /\.thw-stage-done\{--thw-marker-size:12px\}/);
  assert.match(css, /\.thw-stage::before\{[^}]*left:0;[^}]*transform:translate\(-50%,-50%\)/);
  assert.match(css, /\.thw-stage:not\(:last-child\)::after\{[^}]*left:-1px;width:2px/);
  assert.doesNotMatch(css, /\.thw-stages-horizontal::before\{/);
  assert.match(css, /\.thw-stages-horizontal>\.thw-stage\{[^}]*text-align:center\}/);
  assert.match(css, /\.thw-stages-horizontal>\.thw-stage::before\{top:8px;left:50%\}/);
  assert.match(css, /\.thw-stages-horizontal>\.thw-stage:not\(:last-child\)::after\{top:7px;bottom:auto;left:50%;width:calc\(100% \+ 12px\);height:2px\}/);
  assert.match(css, /@media\(max-width:680px\)[\s\S]*\.thw-stages-horizontal>\.thw-stage::before\{left:0\}/);
  assert.match(css, /@media\(max-width:680px\)[\s\S]*\.thw-stages-horizontal>\.thw-stage:not\(:last-child\)::after\{top:8px;bottom:-8px;left:-1px;width:2px;height:auto\}/);
  assert.match(css, /@keyframes thw-spin\{to\{transform:translate\(-50%,-50%\) rotate\(360deg\)/);
});

test('section headings omit redundant small task counts', () => {
  assert.doesNotMatch(source, /<span>\{(?:attention|running)\.length\} 项<\/span>/);
  assert.doesNotMatch(css, /\.thw-section-head>span\{/);
});

test('running cards place type, title and action in one compact header', () => {
  const card = source.slice(source.indexOf('<article className="thw-running-card"'), source.indexOf('</article>)', source.indexOf('<article className="thw-running-card"')));
  assert.ok(card.indexOf('<KindTag') < card.indexOf('<h3>'));
  assert.ok(card.indexOf('<h3>') < card.indexOf('查看任务'));
  assert.ok(card.indexOf('查看任务') < card.indexOf('<StageList'));
  assert.match(css, /\.thw-running-head\{display:flex;align-items:center;justify-content:space-between/);
  assert.match(css, /\.thw-running-identity\{display:flex;align-items:center;gap:12px;min-width:0\}/);
  assert.doesNotMatch(card, /<StageList[^>]*\/>\s*<Link/);
});

test('running tasks occupy full-width rows and all stages fit one line on desktop', () => {
  const card = source.slice(source.indexOf('<article className="thw-running-card"'), source.indexOf('</article>)', source.indexOf('<article className="thw-running-card"')));
  assert.match(card, /<StageList stages=\{task\.stages\} horizontal \/>/);
  assert.match(source, /'--thw-stage-count': stages\.length/);
  assert.match(css, /\.thw-stages-horizontal\{grid-template-columns:repeat\(var\(--thw-stage-count,4\),minmax\(0,1fr\)\)\}/);
  assert.match(css, /\.thw-running-grid\{grid-template-columns:minmax\(0,1fr\);gap:14px\}/);
  assert.match(css, /\.thw-stages-horizontal>\.thw-stage:last-child::after\{content:none\}/);
  assert.match(css, /@media\(max-width:1200px\)\{\.thw-running-grid\{grid-template-columns:1fr/);
  assert.match(css, /@media\(max-width:680px\)\{\.thw-running-grid\{grid-template-columns:1fr\}\.thw-stages-horizontal\{grid-template-columns:1fr/);
});

test('published frontend version matches the package and lockfile', () => {
  assert.equal(packageJson.version, '0.5.4');
  assert.equal(packageLock.version, packageJson.version);
  assert.equal(packageLock.packages[''].version, packageJson.version);
  assert.match(shell, new RegExp(`th-app-version">v${packageJson.version.replaceAll('.', '\\.')}`));
});

test('task navigation links share an outlined button treatment', () => {
  assert.match(source, /thw-running-head[^\n]*<Link className="thw-action-button" to="\/tasks">查看任务/);
  assert.match(source, /<Link className="thw-action-button" to="\/tasks">查看全部任务/);
  assert.match(source, /<Link className="thw-action-button" to="\/tasks">\{task\.kind === 'analysis' \? '查看证据' : '查看结果'\}/);
  assert.match(css, /\.thw-workbench a\.thw-action-button\{[^}]*border:1px solid var\(--thw-line\)/);
  assert.match(css, /\.thw-workbench a\.thw-action-button:focus-visible\{outline:2px/);
});

test('workbench actions have no decorative arrows', () => {
  assert.doesNotMatch(source, /→|➜|➡|&rarr;|arrow-right/);
  for (const label of ['导入 Trace', '运行分析', '设计评测', '查看原因', '查看任务', '查看全部任务', '查看证据', '查看结果', '导入第一批 Trace']) {
    assert.ok(source.includes(label));
  }
});

test('local demo shows four running task rows without changing the real-data limit', () => {
  assert.match(source, /id: 'demo-analysis-running'.*kind: 'analysis'.*state: 'running'/);
  assert.match(source, /id: 'demo-doubao-import'.*kind: 'adapter_import'.*state: 'running'/);
  assert.match(source, /\.slice\(0, demoMode \? 4 : 2\)/);
  assert.match(source, /kind: 'evaluation', title: '工具调用准确性评测', state: 'running'/);
});
