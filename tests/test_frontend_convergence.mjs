import assert from 'node:assert/strict';
import { access, readFile } from 'node:fs/promises';
import test from 'node:test';

const root = new URL('../apps/web/src/', import.meta.url);
const legacyFiles = [
  'components/AnalysisPanel.tsx',
  'components/BatchAnalysis.tsx',
  'components/BatchWorkspace.tsx',
  'components/BetaAppShell.tsx',
  'components/BetaWorkspace.tsx',
  'components/ClassificationTiming.tsx',
  'components/CreateCollectionDialog.tsx',
  'components/Evaluations.tsx',
  'components/GettingStarted.tsx',
  'components/HomePage.tsx',
  'components/ImportDialog.tsx',
  'components/PluginTools.tsx',
  'components/PluginsPage.tsx',
  'components/StageMetrics.tsx',
  'components/TraceExplorer.tsx',
  'components/WorkspacePages.tsx',
  'styles.css',
  'guides.css',
  'batch.css',
  'evaluations.css',
  'plugins.css',
  'plugin-directory.css',
  'api/client.ts',
  'api/batch-client.ts',
  'api/evaluations.ts',
  'api/extensions.ts',
  'api/http-client.ts',
  'api/infra-generated.ts',
  'api/mock.ts',
  'plugins/host/react.tsx',
];

test('前端只保留当前工作台实现', async () => {
  const app = await readFile(new URL('App.tsx', root), 'utf8');
  const shell = await readFile(new URL('components/AppShell.tsx', root), 'utf8');
  const pages = await readFile(new URL('components/InfraPages.tsx', root), 'utf8');
  const catalog = await readFile(new URL('skills/catalog.ts', root), 'utf8');
  const main = await readFile(new URL('main.tsx', root), 'utf8');
  const styles = await readFile(new URL('workspace.css', root), 'utf8');
  const pluginStyles = await readFile(new URL('components/Plugins.css', root), 'utf8');
  const generated = await readFile(new URL('api/generated.ts', root), 'utf8');

  assert.match(app, /parts\[0\] === 'beta'/, '旧地址应仅保留重定向兼容');
  assert.doesNotMatch(`${shell}\n${pages}\n${catalog}`, /\/beta(?:\/|['"`])/, '现行页面不能再生成 Beta 地址');
  assert.doesNotMatch(shell, /分析集/, '前端应统一使用“实验”');
  assert.match(shell, /className="th-assistant" aria-label="AI（开发中）" title="AI 功能开发中" disabled/, 'AI 入口应明确禁用');
  assert.match(shell, /\/>AI<\/button>/, 'AI 入口应显示简洁标签');
  assert.doesNotMatch(shell, /<kbd>/, 'AI 入口不应显示快捷键');
  assert.match(shell, /className="th-app-version">v0\.1<\/span>/, '侧栏底部应标注 v0.1');
  assert.match(shell, />导入 Trace<\/Link>/, '首页主操作应明确表示导入 Trace');
  assert.doesNotMatch(pluginStyles, /line-clamp|overflow:hidden[^}]*\.th-plugin-manager-copy>span:last-child/, 'Skill 说明不能截断');
  assert.match(pluginStyles, /\.th-plugin-manager-copy>span:last-child\{[^}]*text-align:center[^}]*text-wrap:balance/, 'Skill 说明应居中并均衡换行');
  assert.match(shell, /item\.kind==='skill'\?<span className="th-plugin-manager-status">开发中<\/span>/, '所有内置 Skill 卡片都应显示开发中');
  assert.match(catalog, /'trace-hunter-adapter':'\/plugins\/adapter'/, 'Adapter 仓库 Skill 应复用专属详情入口');
  assert.match(shell, /detail:skillCardDescriptions\[item\.id\]\|\|item\.description/, 'Skill 卡片应复用仓库说明而不是通用占位文案');
  assert.doesNotMatch(shell, /\.\.\.repositorySkills\.map[\s\S]*key:\s*'trace-hunter-adapter'/, 'Adapter 不得在仓库目录之外再次手工追加');
  assert.match(shell, /status='开发中'/, 'Skill 详情页默认状态应为开发中');
  assert.doesNotMatch(shell, /status="已保存"|status='Infra 内置'/, 'Skill 详情页不应保留旧状态文案');
  assert.match(styles, /@media\(min-width:761px\)\{\.th-sidebar\{width:208px\}\.th-main\{margin-left:208px\}\}/, '桌面侧栏应收窄且主内容同步对齐');
  assert.doesNotMatch(shell, /demoCollectionRows|新建实验|运行评测|api\.collection|api\.case|api\.compare/, '静态实验与无行为控件必须移除');
  assert.match(shell, /props\.section === 'collections' \|\| props\.section === 'cases'[^\n]*Navigate to="\/traces"/, '旧实验地址必须安全重定向');
  assert.match(shell, /label: '查询与分析'/, 'Trace Infra 查询能力必须有可操作入口');
  assert.doesNotMatch(main, /BrowserPluginsProvider|styles\.css|guides\.css/, '入口不能再挂载旧版宿主和样式');
  assert.doesNotMatch(generated, /"\/api\/(?:collections|cases|compare|extensions|evaluations|plugin-runs)/, '标准生成类型不能保留旧接口');
  assert.doesNotMatch(styles, /beta|th-exit-beta|th-minimal-|th-sidebar-recent|th-plugin-grid|th-eval-/i, '主样式不能保留旧版选择器');

  for (const file of legacyFiles) {
    await assert.rejects(access(new URL(file, root)), undefined, `旧版文件仍然存在：${file}`);
  }
});
