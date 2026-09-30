// Publish an explicit documentation set. Never walk var/, source captures or secrets.
import { readFile, writeFile, mkdir, rm } from 'node:fs/promises';
import { resolve, dirname, relative, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const target = resolve(root, 'apps/web/public/docs');
const files = [
  'project/system-design.html', 'research/system-construction-evidence.md',
  'project/platform-introduction-brief-20260918.md',
  'reports/platform-case-flow-20260918.md',
  'reports/platform-case-flow-20260918.json',
  'reports/platform-case-coverage-20260920.md', 'reports/platform-case-coverage-20260920.json',
  'prototypes/evidence-collection-mvp/overview.md', 'prototypes/evidence-collection-mvp/acceptance.md',
  'prototypes/evidence-collection-mvp/design.md', 'prototypes/evidence-collection-mvp/prototype.pseudo',
  'prototypes/evidence-collection-mvp/unified-agent.md', 'prototypes/evidence-collection-mvp/unified-agent.pseudo',
  'prototypes/evidence-collection-mvp/check_fixtures.py',
  'prototypes/evidence-collection-mvp/platform-flow.pseudo',
  'prototypes/evidence-collection-mvp/property-inspection.example.json',
  'prototypes/evidence-collection-mvp/record-level.example.json',
  'prototypes/trace-query-mvp/design.md', 'prototypes/trace-query-mvp/prototype.pseudo',
  'prototypes/trace-query-mvp/example.json', 'research/trace-search-provenance-causality-20260916.md',
  'research/mimo-online-rl-20260918.md', 'research/async-ppo-grpo-evidence-20260918.md',
  'project/system-guide-design-brief-20260915.md', 'research/annotation-evidence-20260915.md',
  'research/product-background-20260915.md',
  'prototypes/trace-analysis-mvp/design.md', 'prototypes/trace-analysis-mvp/prototype.pseudo',
  'prototypes/trace-analysis-mvp/example.json', 'api/selections.md', 'api/execution.md',
  'diagrams/system-current.svg', 'diagrams/system-target.svg', 'diagrams/system-resources.svg',
  'diagrams/system-ingest.svg', 'diagrams/system-analyze.svg', 'diagrams/system-replay.svg',
  'project/overview.md', 'project/release-20260914.md', 'research/neutral-trace-core.md', 'research/README.md', 'roadmap.md',
  'research/langfuse-traces-backend-architecture-20260912.md', 'research/multi-turn-traces.md',
  'research/trajectory-sources-2026-09-10.json', 'research/interop-validation-2026-09-10.json',
  'research/plugin-composition-from-dsh.md', 'research/agent-hub-architecture.md', 'research/agent-hub-sources-2026-09-10.json',
  'research/data-loop-audit.md', 'research/doubao-export-and-capture.md', 'research/doubao-token-recovery-strategy.md',
  'fornax-system-design-and-public-code.md', 'fornax-code-deep-dive.md', 'fornax-trace-recording-and-indexing.md',
  'trajectory-record-diagnose-repair-first-principles.md', 'reviews/platform-architecture-20260911.md',
  'architecture/backend-target-architecture.md', 'architecture/service-boundaries.md', 'architecture/training-data-platform.md',
  'architecture/general-plugin-model.md', 'architecture/browser-plugin-host.md',
  'api/README.md', 'api/trace-revisions.md', 'api/trace-query.md', 'api/trace-aggregates.md',
  'api/evaluation-surface.md', 'api/remaining-endpoints.md', 'api/advanced-query.md', 'api/cli.md', 'api/tasks.md', 'api/import-progress.md',
  'api/artifacts.md', 'api/invocations.md', 'api/remote-execution.md', 'api/external-access.md', 'api/task-access.md',
  'guides/general-plugins.md', 'guides/base-stage-classification.md', 'guides/standard-trace-export.md',
  'implementation/backend-core-stage-1.md', 'reports/call-duration-bands-20260911.md',
];
const allowed = new Set(files.map(name => resolve(root, 'docs', name)));
const sources = await Promise.all(files.map(async name => ({ name, raw: await readFile(resolve(root, 'docs', name)) })));
// One product introduction; the system design remains a separate technical document.
const introduction = await readFile(resolve(root, 'docs/project/platform-introduction.html'));
await writeFile(resolve(root, 'apps/web/public/about.html'), introduction);
await writeFile(resolve(root, 'apps/web/public/platform.html'), `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trace Hunter · 平台介绍</title><link rel="canonical" href="/about.html">
<script src="/platform-redirect.js"></script><noscript><meta http-equiv="refresh" content="0;url=/about.html"></noscript>
</head><body><p>平台介绍已统一到 <a href="/about.html">Trace Hunter 平台介绍</a>。</p></body></html>\n`);
await rm(target, { recursive: true, force: true });
const manifest = [];
for (const { name, raw } of sources) {
  let content = raw;
  if (extname(name) === '.md') {
    const directory = dirname(resolve(root, 'docs', name));
    content = Buffer.from(raw.toString('utf8').replace(/\[([^\]\n]+)\]\(([^)\n]+)\)/g, (whole, label, link) => {
      if (/^(https?:|#)/.test(link)) return whole;
      const [path, fragment] = link.split('#');
      const resolved = resolve(directory, path);
      if (allowed.has(resolved)) return `[${label}](/docs/${relative(resolve(root, 'docs'), resolved)}${fragment ? '#' + fragment : ''})`;
      // The original repository docs retain code/experiment references; no dead links or private local paths in the web copy.
      return label + '（见仓库文档）';
    }));
  }
  const destination = resolve(target, name);
  await mkdir(dirname(destination), { recursive: true });
  await writeFile(destination, content);
  manifest.push({ path: '/docs/' + name, source_sha256: createHash('sha256').update(raw).digest('hex'), published_sha256: createHash('sha256').update(content).digest('hex') });
}
await writeFile(resolve(target, 'index.json'), JSON.stringify({ kind: 'trace-hunter-docs', documents: manifest }, null, 2) + '\n');
console.log(`Published ${manifest.length} curated documents; raw trace data excluded.`);
