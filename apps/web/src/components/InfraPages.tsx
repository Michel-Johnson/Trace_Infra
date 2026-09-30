import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import {
  infraApi, spanFields, traceFields,
  type ImportBatch, type InfraProject, type InfraTask, type ObservabilityReport, type UploadStatus,
  type SearchCapabilities, type SearchField, type SearchItem, type SearchMode, type SearchObjectKind,
  type RawTraceDocument, type RawTraceSpan, type SearchResult, type SpanFilters, type SpanItem, type TraceItem,
} from '../api/infra-client';
import { cacheKey, cacheKeyPrefix, invalidateCachedQueries, loadCachedQuery, peekCachedQuery, setCachedQuery } from '../api/query-cache';
import { WorkspaceIcon as Icon } from './WorkspaceIcon';
import { WorkspaceSelect } from './WorkspaceSelect';
import { clampTraceRange, intersectTraceRange, moveTraceRange, resizeTraceRange, traceSpanIntersectsRange, traceTimelineUsesDuration, type TraceRange } from '../lib/trace-timeline-range';
import { groupInfraTasks, taskBatchId, type InfraTaskGroup } from '../lib/task-groups';
import { linkTasksToAgentSessions, loadTaskAgentSessions, taskAgentHref } from '../lib/task-agent-link';
import { sha256Hex } from '../lib/sha256';
import { duplicateImportFromCode, duplicateImportNotice, reusedSuccessfulImport, type DuplicateImportState } from '../lib/import-duplicate-status';
import { agentTraceGroupId, agentTraceProjects, mergeProjectTraces, traceProjectOptions, traceProjectSelection, withAgentTraceLabels, type ProjectTraceItem } from '../lib/agent-trace-projects';
import { searchAgentTraceProjects, type ProjectSearchResult } from '../lib/agent-trace-search';
import './InfraImport.css';
import './InfraTasks.css';

const importStepLabels = {
  format_inspect: 'Agent 识别格式', adapter_select: '选择或开发 Adapter',
  adapter_validate: '字段与来源校验', trace_import: '导入 Trace', readback: '回读结果',
};

function loadProjects(force = false) {
  return loadCachedQuery(cacheKey('projects-all'), async () => {
    const items: InfraProject[] = [];
    const seen = new Set<string>();
    let after: string | null = null;
    do {
      const page = await infraApi.projects(undefined, after);
      items.push(...page.items);
      after = page.next_after;
      if (after && seen.has(after)) throw new Error('项目分页游标重复，无法完整列出项目');
      if (after) seen.add(after);
    } while (after);
    return { items, next_after: null };
  }, { ttlMs: 30_000, force });
}

function useInfraProject(allowAgentGroup = false, initialProject = '') {
  const cached = peekCachedQuery<{ items: InfraProject[] }>(cacheKey('projects-all'));
  const [projects, setProjects] = useState<InfraProject[]>(cached?.items || []);
  const [project, setProjectState] = useState(() => {
    const stored = initialProject || localStorage.getItem('trace-hunter-infra-project') || 'sample-10';
    return !allowAgentGroup && stored === agentTraceGroupId ? 'sample-10' : stored;
  });
  const [projectError, setProjectError] = useState('');
  const refreshProjects = useCallback(async (force = false) => {
    const value = await loadProjects(force);
    setProjects(value.items);
    return value.items;
  }, []);
  useEffect(() => {
    let active = true;
    loadProjects().then(value => {
      if (!active) return;
      setProjects(value.items);
      if (!value.items.some(item => item.project_id === project) && !(allowAgentGroup && project === agentTraceGroupId)) {
        const next = value.items.find(item => item.project_id === 'sample-10')?.project_id || value.items[0]?.project_id || '';
        setProjectState(next);
      }
    }).catch(error => { if (active) setProjectError(error.message); });
    return () => { active = false; };
  }, []);
  const setProject = (value: string) => { localStorage.setItem('trace-hunter-infra-project', value); setProjectState(value); };
  return { projects, project, setProject, projectError, refreshProjects };
}

function ProjectBar({ projects, project, setProject, children, options }: { projects: InfraProject[]; project: string; setProject: (value: string) => void; children?: ReactNode; options?: Array<{value: string; label: string}> }) {
  return <div className="th-infra-toolbar"><div className="th-project-picker"><span>项目</span><WorkspaceSelect value={project} options={options || projects.map(item => ({ value: item.project_id, label: item.name }))} onChange={setProject} ariaLabel="项目" /></div><div>{children}</div></div>;
}

function ErrorNotice({ value }: { value: string }) {
  return value ? <div className="th-infra-error" role="alert">{value}</div> : null;
}

async function allSpans(project: string, filters: SpanFilters = {}) {
  return loadCachedQuery(cacheKey('span-list', project, filters), async () => {
    const items: SpanItem[] = [];
    let cursor: string | null = null;
    do {
      const page = await infraApi.spans(project, { filters, fields: spanFields, revisions: 'latest', order: 'source', limit: 100, cursor });
      items.push(...page.items); cursor = page.next_cursor;
    } while (cursor && items.length < 5000);
    return items;
  }, { ttlMs: 30_000 });
}

async function allTraces(project: string, force = false) {
  return loadCachedQuery(cacheKey('trace-list', project), async () => {
    const items: TraceItem[] = [];
    let cursor: string | null = null;
    do {
      const page = await infraApi.traces(project, { fields: traceFields, revisions: 'latest', limit: 100, cursor });
      items.push(...page.items); cursor = page.next_cursor;
    } while (cursor && items.length < 5000);
    return items;
  }, { ttlMs: 30_000, force });
}

async function allAgentTraces(projects: InfraProject[], force = false): Promise<ProjectTraceItem[]> {
  const selected = agentTraceProjects(projects);
  let index = 0;
  const groups: Array<{ project_id: string; items: TraceItem[] }> = [];
  await Promise.all(Array.from({ length: Math.min(6, selected.length) }, async () => {
    while (index < selected.length) {
      const project = selected[index++];
      groups.push({ project_id: project.project_id, items: await allTraces(project.project_id, force) });
    }
  }));
  return loadAgentTraceLabels(mergeProjectTraces(groups));
}

async function loadAgentTraceLabels(rows: ProjectTraceItem[]): Promise<ProjectTraceItem[]> {
  const identities = rows.flatMap(row => row.run_id ? [{ project_id: row.project_id, run_id: row.run_id }] : []);
  if (!identities.length) return rows;
  try {
    const labels: Array<{ project_id: string; run_id: string; title: string }> = [];
    for (let offset = 0; offset < identities.length; offset += 200) {
      const page = await infraApi.agentTraceLabels(identities.slice(offset, offset + 200));
      labels.push(...page.items);
    }
    return withAgentTraceLabels(rows, labels);
  } catch { return rows; }
}

function invalidateProjectCache(project: string) {
  for (const namespace of ['trace-list', 'span-list', 'search', 'observability']) {
    invalidateCachedQueries(cacheKeyPrefix(namespace, project));
  }
}

function Highlight({ hit }: { hit: SearchItem }) {
  const characters = Array.from(hit.snippet);
  const parts: ReactNode[] = [];
  let cursor = 0;
  hit.match_ranges.forEach((range, index) => {
    if (range.start > cursor) parts.push(characters.slice(cursor, range.start).join(''));
    parts.push(<mark key={index}>{characters.slice(range.start, range.end).join('')}</mark>);
    cursor = range.end;
  });
  if (cursor < characters.length) parts.push(characters.slice(cursor).join(''));
  return <>{hit.snippet_truncated && hit.snippet_start > 0 ? '…' : ''}{parts}{hit.snippet_truncated ? '…' : ''}</>;
}

type SearchFiltersState = { skill: string; tool: string; status: string; field: SearchField | ''; kind: SearchObjectKind | '' };
type SearchMetrics = { mode: SearchMode; totalCount: number; traceCount: number; hits: number; truncated: boolean; elapsedMs: number };
type CachedSearch = { result: ProjectSearchResult; metadata: Map<string, SpanItem>; elapsedMs: number };
const searchInputKey = 'trace-hunter-search-input-v1';
const defaultSearchFilters: SearchFiltersState = { skill: '', tool: '', status: '', field: '', kind: '' };

function storedSearchInput(): { query: string; mode: SearchMode; filters: SearchFiltersState } {
  try {
    const value = JSON.parse(sessionStorage.getItem(searchInputKey) || '{}') as Partial<{ query: string; mode: SearchMode; filters: SearchFiltersState }>;
    return {
      query: typeof value.query === 'string' ? value.query : 'tool',
      mode: value.mode === 'regex' ? 'regex' : 'literal',
      filters: { ...defaultSearchFilters, ...(value.filters || {}) },
    };
  } catch { return { query: 'tool', mode: 'literal', filters: defaultSearchFilters }; }
}

export function InfraSearchPage() {
  const { projects, project, setProject, projectError } = useInfraProject(true);
  const selectedProject = traceProjectSelection(projects, project);
  const projectOptions = useMemo(() => traceProjectOptions(projects), [projects]);
  const agentProjects = useMemo(() => agentTraceProjects(projects).map(item => item.project_id), [projects]);
  const initialSearch = useMemo(storedSearchInput, []);
  const [capabilities, setCapabilities] = useState<SearchCapabilities>();
  const [query, setQuery] = useState(initialSearch.query);
  const [mode, setMode] = useState<SearchMode>(initialSearch.mode);
  const [filters, setFilters] = useState<SearchFiltersState>(initialSearch.filters);
  const [result, setResult] = useState<ProjectSearchResult>();
  const [metadata, setMetadata] = useState(new Map<string, SpanItem>());
  const [elapsedMs, setElapsedMs] = useState<number>();
  const [cursor, setCursor] = useState<string | null>(null);
  const [cursorStack, setCursorStack] = useState<Array<string | null>>([]);
  const [comparison, setComparison] = useState<SearchMetrics[]>([]);
  const [traceTotal, setTraceTotal] = useState<number>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const requestSeq = useRef(0);

  useEffect(() => {
    try { sessionStorage.setItem(searchInputKey, JSON.stringify({ query, mode, filters })); } catch { /* Session storage is an optimization only. */ }
  }, [query, mode, filters]);

  useEffect(() => {
    let active = true;
    loadCachedQuery(cacheKey('search-capabilities'), () => infraApi.searchCapabilities(), { ttlMs: 300_000 })
      .then(value => { if (active) setCapabilities(value); })
      .catch(reason => { if (active) setError(reason.message); });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    if (!selectedProject) { setTraceTotal(undefined); return; }
    let active = true;
    if (selectedProject === agentTraceGroupId) {
      setTraceTotal(undefined);
      Promise.all(agentProjects.map(projectId => allTraces(projectId)))
        .then(groups => { if (active) setTraceTotal(groups.reduce((total, rows) => total + rows.length, 0)); })
        .catch(reason => { if (active) setError(reason.message); });
    } else {
      const cached = peekCachedQuery<TraceItem[]>(cacheKey('trace-list', selectedProject));
      setTraceTotal(cached?.length);
      allTraces(selectedProject).then(items => { if (active) setTraceTotal(items.length); }).catch(reason => { if (active) setError(reason.message); });
    }
    return () => { active = false; };
  }, [selectedProject, agentProjects]);

  const runSearch = useCallback(async (nextCursor: string | null = null, overrideMode?: SearchMode) => {
    if (!selectedProject || !query.trim() || (selectedProject === agentTraceGroupId && !agentProjects.length)) return;
    const sequence = ++requestSeq.current;
    const selectedMode = overrideMode || mode;
    const searchFilters = {
      ...(filters.skill.trim() ? { skill_name: [filters.skill.trim()] } : {}),
      ...(filters.tool.trim() ? { name: [filters.tool.trim()] } : {}),
      ...(filters.status ? { status: [filters.status] } : {}),
      ...(filters.field ? { field: [filters.field] } : {}),
      ...(filters.kind ? { object_kind: [filters.kind] } : {}),
    };
    const key = cacheKey('search', selectedProject, agentProjects, query.trim(), selectedMode, searchFilters, nextCursor);
    const cached = peekCachedQuery<CachedSearch>(key);
    if (cached) {
      setResult(cached.result); setMetadata(cached.metadata); setElapsedMs(cached.elapsedMs); setCursor(nextCursor);
    }
    setBusy(!cached); setError('');
    try {
      const loaded = await loadCachedQuery(key, async () => {
        const started = performance.now();
        const request = (projectId: string, cursor: string | null, limit: number) => infraApi.search(projectId, {
          query: query.trim(), mode: selectedMode, scope: 'analysis', filters: searchFilters,
          revisions: 'latest', limit, cursor,
        });
        const value: ProjectSearchResult = selectedProject === agentTraceGroupId
          ? await searchAgentTraceProjects(agentProjects, nextCursor, request)
          : await request(selectedProject, nextCursor, 20);
        const visibleItems = value.items;
        const projectHits = new Map<string, string[]>();
        for (const item of visibleItems) {
          const source = item.project_id || selectedProject;
          if (!projectHits.has(source)) projectHits.set(source, []);
          projectHits.get(source)?.push(item.run_id);
        }
        const spanGroups = await Promise.all(Array.from(projectHits, async ([source, runIds]) => ({
          source, spans: await allSpans(source, { run_id: [...new Set(runIds)] }),
        })));
        return {
          result: { ...value, items: visibleItems },
          metadata: new Map(spanGroups.flatMap(({ source, spans }) => spans.flatMap(item =>
            item.span_id ? [[`${source}:${item.run_id}:${item.span_id}`, item] as const] : []))),
          elapsedMs: performance.now() - started,
        };
      }, { ttlMs: 30_000 });
      if (sequence !== requestSeq.current) return;
      setMetadata(loaded.metadata); setResult(loaded.result); setElapsedMs(loaded.elapsedMs); setCursor(nextCursor);
    } catch (reason) { setError((reason as Error).message); }
    finally { if (sequence === requestSeq.current) setBusy(false); }
  }, [selectedProject, agentProjects, query, mode, filters.skill, filters.tool, filters.status, filters.field, filters.kind]);

  useEffect(() => { setResult(undefined); setComparison([]); setCursor(null); setCursorStack([]); }, [selectedProject]);
  useEffect(() => { if (selectedProject && capabilities && !result && !busy) void runSearch(); }, [selectedProject, agentProjects, capabilities, result, busy, runSearch]);

  async function compareModes() {
    if (!selectedProject || !query.trim()) return;
    setBusy(true); setError(''); setComparison([]);
    try {
      const searchFilters = {
        ...(filters.skill.trim() ? { skill_name: [filters.skill.trim()] } : {}),
        ...(filters.tool.trim() ? { name: [filters.tool.trim()] } : {}),
        ...(filters.status ? { status: [filters.status] } : {}),
        ...(filters.field ? { field: [filters.field] } : {}),
        ...(filters.kind ? { object_kind: [filters.kind] } : {}),
      };
      const rows: SearchMetrics[] = [];
      for (const nextMode of ['literal','regex'] as const) {
        const started = performance.now();
        const request = (projectId: string, cursor: string | null, limit: number) => infraApi.search(projectId, {
          query: query.trim(), mode: nextMode, scope: 'analysis', filters: searchFilters,
          revisions: 'latest', limit, cursor,
        });
        const value = selectedProject === agentTraceGroupId
          ? await searchAgentTraceProjects(agentProjects, null, request)
          : await request(selectedProject, null, 20);
        rows.push({ mode: nextMode, totalCount: value.total_count, traceCount: value.matched_trace_count, hits: value.items.length, truncated: value.truncated, elapsedMs: performance.now() - started });
      }
      setComparison(rows);
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }

  const submit = () => { setCursorStack([]); void runSearch(null); };
  const next = () => { if (result?.next_cursor) { setCursorStack(stack => [...stack, cursor]); void runSearch(result.next_cursor); } };
  const previous = () => { const target = cursorStack.at(-1) ?? null; setCursorStack(stack => stack.slice(0, -1)); void runSearch(target); };

  return <div className="th-infra-page th-search-page">
    <ProjectBar projects={projects} project={selectedProject} setProject={setProject} options={projectOptions}><span className="th-live-source">真实 API</span></ProjectBar>
    <ErrorNotice value={projectError || error} />
    <section className="th-search-console">
      <div className="th-search-query"><WorkspaceSelect value={mode} options={(capabilities?.modes || ['literal','regex']).map(value => ({ value, label: value }))} onChange={setMode} ariaLabel="搜索模式"/><input value={query} onChange={event => setQuery(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') submit(); }} placeholder="搜索 Span、Skill、Tool 或正文"/><button disabled={busy} onClick={submit}><Icon name="search" size={16}/>{busy ? '查询中' : '搜索'}</button><button disabled={busy} onClick={() => void compareModes()}>两模式对比</button></div>
      <div className="th-search-filters"><label>Skill<input value={filters.skill} onChange={event => setFilters(value => ({ ...value, skill: event.target.value }))} placeholder="精确名称"/></label><label>Tool<input value={filters.tool} onChange={event => setFilters(value => ({ ...value, tool: event.target.value }))} placeholder="精确名称"/></label><label>状态<WorkspaceSelect<string> value={filters.status} options={[{value:'',label:'全部'},{value:'ok',label:'ok'},{value:'error',label:'error'},{value:'unknown',label:'unknown'}]} onChange={status => setFilters(value => ({ ...value, status }))} ariaLabel="状态"/></label><label>正文区域<WorkspaceSelect<SearchField | ''> value={filters.field} options={[{value:'',label:'全部'},...(capabilities?.fields || []).map(value => ({value,label:value}))]} onChange={field => setFilters(value => ({ ...value, field }))} ariaLabel="正文区域"/></label><label>对象<WorkspaceSelect<SearchObjectKind | ''> value={filters.kind} options={[{value:'',label:'全部'},...(capabilities?.object_kinds || []).map(value => ({value,label:value}))]} onChange={kind => setFilters(value => ({ ...value, kind }))} ariaLabel="对象"/></label></div>
    </section>
    <section className="th-search-diagnostics" aria-label="搜索诊断"><Metric label="Trace 总数" value={traceTotal == null ? '读取中' : String(traceTotal)} /><Metric label="命中总数" value={String(result?.total_count ?? '—')} /><Metric label="涉及 Trace" value={String(result?.matched_trace_count ?? '—')} /><Metric label="Backend" value={capabilities?.backend || '读取中'} /><Metric label="模式" value={mode} /><Metric label="请求耗时" value={elapsedMs == null ? '—' : `${elapsedMs.toFixed(1)} ms`} /></section>
    {comparison.length > 0 && <section className="th-mode-comparison">{comparison.map(item => <article key={item.mode}><strong>{item.mode}</strong><span>{item.totalCount} 总命中</span><span>{item.traceCount} Trace</span><span>本页 {item.hits}</span><span>{item.elapsedMs.toFixed(1)} ms</span><em>{item.truncated ? '有下一页' : '最后一页'}</em></article>)}<p>命中总数由完整搜索条件计算；分页只控制本页展示。</p></section>}
    <section className="th-search-results">{result?.items.map((hit, index) => { const source = hit.project_id || selectedProject; const span = hit.span_id ? metadata.get(`${source}:${hit.run_id}:${hit.span_id}`) : undefined; return <article key={`${source}:${hit.run_id}:${hit.revision}:${hit.object_id}:${hit.field}:${index}`}><header><div><span>{hit.object_kind}</span><strong>{hit.run_id}</strong><code>{hit.span_id || hit.object_id}</code></div><Link to={`/traces/${encodeURIComponent(hit.run_id)}?project=${encodeURIComponent(source)}&revision=${hit.revision}${hit.span_id ? `&span=${encodeURIComponent(hit.span_id)}` : ''}`}>查看 Trace <Icon name="arrow" size={14}/></Link></header><div className="th-hit-meta"><span>{hit.field}</span><span>{span?.status || '状态未提供'}</span><span>{span?.duration_ms == null ? '耗时未记录' : `${span.duration_ms.toFixed(0)} ms`}</span><span>score {hit.score.toFixed(3)}</span><span>{hit.text_state}</span></div><pre><Highlight hit={hit}/></pre><footer>{hit.source_refs.length ? hit.source_refs.map(ref => <code key={`${ref.source_id}:${ref.pointer}`}>{ref.source_id || 'source'} {ref.pointer}</code>) : <span>无 source refs</span>}</footer></article>; })}{result && !result.items.length && <div className="th-infra-empty"><strong>没有命中</strong><span>当前过滤条件或查询模式未返回结果。</span></div>}</section>
    {result && <div className="th-page-controls"><button disabled={!cursorStack.length || busy} onClick={previous}>上一页</button><span>Keyset 分页 · {result.projector_version}</span><button disabled={!result.next_cursor || busy} onClick={next}>下一页</button></div>}
  </div>;
}

type InfraOperation = 'objects' | 'window' | 'metrics' | 'evidence';
const infraOperationOptions: Array<{ value: InfraOperation; label: string }> = [
  { value: 'objects', label: '对象证据' }, { value: 'window', label: '前后步骤' },
  { value: 'metrics', label: '指标统计' }, { value: 'evidence', label: '批量证据导出' },
];
const infraOperationBodies: Record<InfraOperation, object> = {
  objects: { filters: {}, fields: ['run_id','revision','object_id','object_kind','payload','source_refs'], limit: 100 },
  window: { anchor: { run_id: '', revision: 1, span_id: '' }, before: 20, after: 20, include: ['documents','related_objects','edges'], preview_chars: 512 },
  metrics: { query: {}, metrics: ['count'], group_by: ['operation'], interval_seconds: null, histogram: [], exemplars: 3 },
  evidence: { title: '批量证据导出', query: {}, fields: [], include_documents: true, page_size: 500 },
};

export function InfraQueryPage() {
  const { projects, project, setProject, projectError } = useInfraProject();
  const [operation, setOperation] = useState<InfraOperation>('objects');
  const [body, setBody] = useState(() => JSON.stringify(infraOperationBodies.objects, null, 2));
  const [result, setResult] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  function selectOperation(value: InfraOperation) { setOperation(value); setBody(JSON.stringify(infraOperationBodies[value], null, 2)); setResult(undefined); setError(''); }
  async function execute() {
    if (!project || busy) return;
    setBusy(true); setError('');
    try {
      const parsed = JSON.parse(body) as Record<string, unknown>;
      if (operation === 'evidence') parsed.request_key ||= crypto.randomUUID();
      const value = operation === 'window' ? await infraApi.spanWindow(project, parsed as never)
        : operation === 'metrics' ? await infraApi.metrics(project, parsed as never)
        : operation === 'objects' ? await infraApi.objects(project, parsed as never)
        : await infraApi.evidenceExport(project, parsed as never);
      setResult(value);
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }
  return <div className="th-infra-page th-infra-query-page">
    <ProjectBar projects={projects} project={project} setProject={setProject}><span className="th-live-source">OpenAPI 2.8</span></ProjectBar>
    <ErrorNotice value={projectError || error}/>
    <section className="th-infra-query-console"><header><WorkspaceSelect value={operation} options={infraOperationOptions} onChange={selectOperation} ariaLabel="Infra 操作"/><button disabled={busy} onClick={() => void execute()}>{busy ? '执行中' : '执行'}</button></header><textarea aria-label="请求 JSON" spellCheck={false} value={body} onChange={event => setBody(event.target.value)}/></section>
    {result !== undefined && <section className="th-infra-query-result"><header><strong>返回结果</strong>{typeof result === 'object' && result && 'task_id' in result && <Link to="/tasks">查看任务进度 <Icon name="arrow" size={13}/></Link>}</header><pre>{JSON.stringify(result, null, 2)}</pre></section>}
  </div>;
}

function Metric({ label, value }: { label: string; value: string }) { return <div><span>{label}</span><strong>{value}</strong></div>; }

type DshLane = 'input' | 'model' | 'tools';

function dshLane(span: SpanItem): DshLane {
  const operation = (span.operation || '').toLowerCase();
  const kind = (span.kind || '').toLowerCase();
  if (['read', 'write', 'edit', 'bash', 'skill', 'tool'].some(value => operation.includes(value) || kind.includes(value))) return 'tools';
  if (operation.includes('human') || ['input', 'user', 'prompt'].some(value => kind.includes(value))) return 'input';
  return 'model';
}

function compactDuration(value: number) {
  if (value < 1000) return `${Math.round(value)} ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)} s`;
  return `${Math.floor(value / 60_000)}m ${Math.round((value % 60_000) / 1000)}s`;
}

type TraceInspectorTab = 'summary' | 'preview' | 'raw' | 'source' | 'payload' | 'result' | 'schema' | 'timing';
const inspectorTabsByLane: Record<DshLane, Array<[TraceInspectorTab, string]>> = {
  tools: [['summary', 'Summary'], ['payload', 'Payload'], ['result', 'Result'], ['schema', 'Schema'], ['timing', 'Timing']],
  model: [['summary', 'Summary'], ['preview', 'Preview'], ['raw', 'Raw']],
  input: [['summary', 'Summary'], ['preview', 'Preview'], ['raw', 'Raw'], ['source', 'Source']],
};

function traceStatusLabel(value: string | null) {
  if (value === 'ok' || value === 'complete' || value === 'completed' || value === 'succeeded') return '成功';
  if (value === 'error' || value === 'failed') return '失败';
  if (value === 'running') return '运行中';
  return value || '未记录';
}

function traceRole(span: SpanItem) {
  const lane = dshLane(span);
  return lane === 'tools' ? 'TOOL' : lane === 'model' ? 'ASSISTANT' : 'USER';
}

function readableValue(value: unknown) {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

function tracePreview(value: unknown) {
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    for (const key of ['content', 'text', 'search_text', 'message', 'output', 'result']) {
      if (record[key] != null) return readableValue(record[key]);
    }
  }
  return readableValue(value);
}

function compactPreview(value: unknown, limit = 220) {
  const text = tracePreview(value).replace(/\s+/g, ' ').trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

function modelResponseSummary(value: unknown) {
  if (!value || typeof value !== 'object') return { text: '', thinking: '', usage: null as Record<string, unknown> | null };
  const response = value as Record<string, unknown>;
  const blocks = Array.isArray(response.content) ? response.content : [];
  const text = blocks.filter((block): block is Record<string, unknown> => Boolean(block && typeof block === 'object'))
    .filter(block => block.type === 'text' && typeof block.text === 'string')
    .map(block => block.text as string).join('\n\n');
  const thinking = blocks.filter((block): block is Record<string, unknown> => Boolean(block && typeof block === 'object'))
    .filter(block => block.type === 'thinking' && typeof block.thinking === 'string')
    .map(block => block.thinking as string).join('\n\n');
  const usage = response.usage && typeof response.usage === 'object' ? response.usage as Record<string, unknown> : null;
  return { text, thinking, usage };
}

function TraceValue({ value, empty = '当前记录未提供此字段。' }: { value: unknown; empty?: string }) {
  const text = readableValue(value);
  if (!text) return <p className="th-trace-inspector-empty">{empty}</p>;
  const structured = typeof value === 'object';
  return <pre className={`th-trace-value ${structured ? 'structured' : ''}`}>{text}</pre>;
}

function TraceSpanInspector({ span, raw, context, parentSpan, index, turnNumber, stepNumber, onClose, onSelectSpan, onLoadSource }: {
  span: SpanItem;
  raw?: RawTraceSpan;
  context?: Record<string, unknown>;
  parentSpan?: SpanItem;
  index: number;
  turnNumber?: number;
  stepNumber?: number;
  onClose: () => void;
  onSelectSpan: (spanId: string) => void;
  onLoadSource?: (sourceId: string) => Promise<unknown>;
}) {
  const [tab, setTab] = useState<TraceInspectorTab>('summary');
  const [requestExpanded, setRequestExpanded] = useState(false);
  const [sourceExpanded, setSourceExpanded] = useState(false);
  const [body, setBody] = useState<{ key: string; request?: unknown; response?: unknown; error?: string; loading?: boolean }>({ key: '' });
  const [responsePreview, setResponsePreview] = useState<{ key: string; value?: unknown; error?: string }>({ key: '' });
  const lane = dshLane(span);
  useEffect(() => { setTab('summary'); setRequestExpanded(false); setSourceExpanded(false); }, [span.span_id, lane]);
  const sourceRefs = span.source_refs?.length ? span.source_refs : raw?.source_refs?.length ? raw.source_refs : raw?.source ? [raw.source] : [];
  const pointer = sourceRefs?.[0]?.pointer;
  const input = raw?.input;
  const output = raw?.output ?? raw?.result;
  const schema = raw?.schema;
  const requestId = (context?.request as { ref?: { source_id?: string } } | undefined)?.ref?.source_id;
  const responseId = sourceRefs.find(ref => ref.source_id?.startsWith('body-') && ref.source_id !== requestId)?.source_id || undefined;
  const bodyKey = `${span.span_id || index}:${requestId || ''}:${responseId || ''}`;
  const visibleBody: typeof body = body.key === bodyKey ? body : { key: bodyKey, loading: true };
  const visibleResponse = responsePreview.key === bodyKey ? responsePreview.value : undefined;
  const responseSummary = modelResponseSummary(visibleResponse);
  const tabs = inspectorTabsByLane[lane];
  const selectTab = (next: TraceInspectorTab) => { setTab(next); setRequestExpanded(false); setSourceExpanded(false); };
  useEffect(() => {
    if (lane !== 'model' || !responseId || !onLoadSource) return;
    let active = true;
    setResponsePreview({ key: bodyKey });
    void onLoadSource(responseId).then(value => {
      if (active) setResponsePreview({ key: bodyKey, value });
    }).catch(reason => {
      if (active) setResponsePreview({ key: bodyKey, error: reason instanceof Error ? reason.message : String(reason) });
    });
    return () => { active = false; };
  }, [lane, bodyKey, responseId, onLoadSource]);
  useEffect(() => {
    if (tab !== 'preview' || !requestExpanded || !onLoadSource || !requestId) return;
    let active = true;
    setBody({ key: bodyKey, loading: true });
    void Promise.allSettled([onLoadSource(requestId), responseId ? onLoadSource(responseId) : Promise.resolve(undefined)])
      .then(([request, response]) => {
        if (!active) return;
        const errors = [request, response].filter(result => result.status === 'rejected')
          .map(result => (result as PromiseRejectedResult).reason)
          .map(reason => reason instanceof Error ? reason.message : String(reason));
        setBody({ key: bodyKey,
          request: request.status === 'fulfilled' ? request.value : undefined,
          response: response.status === 'fulfilled' ? response.value : undefined,
          error: errors.join('；') || undefined });
      });
    return () => { active = false; };
  }, [tab, requestExpanded, bodyKey, requestId, responseId, onLoadSource]);
  const sourceLabel = lane === 'input' ? 'User' : raw?.request_id || sourceRefs?.[0]?.source_id || pointer;
  const hierarchyLabel = parentSpan ? dshLane(parentSpan) === 'model' ? 'Assistant Message' : dshLane(parentSpan) === 'input' ? 'User Message' : parentSpan.name || parentSpan.span_id : span.parent_id || '根节点';
  const rows: Array<[string, string]> = [['Status', traceStatusLabel(span.status)]];
  if (lane === 'tools') {
    if (span.kind) rows.push(['Type', span.kind]);
    if (span.operation) rows.push(['Operation', span.operation]);
    if (span.skill_name) rows.push(['Skill', span.skill_name]);
  }
  if (lane === 'input' && span.duration_ms != null) rows.push(['Duration', compactDuration(span.duration_ms)]);
  const usage = raw?.usage && Object.keys(raw.usage).length ? raw.usage : responseSummary.usage;
  const usageRows: Array<[string, string]> = [];
  const outputTokens = usage?.output_tokens;
  if (lane === 'model' && typeof outputTokens === 'number') {
    usageRows.push(['Tokens', `${outputTokens.toLocaleString()} tok`]);
    const details = usage?.output_tokens_details;
    const reasoningTokens = details && typeof details === 'object' ? (details as Record<string, unknown>).thinking_tokens : undefined;
    if (typeof reasoningTokens === 'number') {
      usageRows.push(['Reasoning', `${reasoningTokens.toLocaleString()} tok`]);
      usageRows.push(['Content', `${Math.max(0, outputTokens - reasoningTokens).toLocaleString()} tok`]);
    }
  } else {
    for (const [label, value] of Object.entries(usage || {})) {
      if (typeof value === 'number' || typeof value === 'string') usageRows.push([label.replaceAll('_', ' '), String(value)]);
    }
  }
  const timingRows: Array<[string, string]> = [];
  if (span.start_ms != null) timingRows.push(['Start offset', `${(span.start_ms / 1000).toFixed(1)} s`]);
  if (span.duration_ms != null) timingRows.push(['Total duration', compactDuration(span.duration_ms)]);
  const primaryTiming: Record<string, string> = { ttft_ms: 'TTFT', generation_ms: 'Generation', throughput_tokens_per_second: 'Throughput' };
  for (const [label, value] of Object.entries(raw?.timing || {})) {
    if (value != null && primaryTiming[label]) timingRows.push([primaryTiming[label], String(value)]);
  }
  const reasoning = responseSummary.thinking || raw?.reasoning || raw?.thinking || (output && typeof output === 'object' ? (output as Record<string, unknown>).reasoning || (output as Record<string, unknown>).thinking : undefined);
  const previewValue = lane === 'input' ? input ?? output : lane === 'model' && responseSummary.text ? responseSummary.text : output ?? input;
  const previewText = compactPreview(previewValue, 900) || span.skill_action || (responseId && !responsePreview.error ? '正在读取模型响应…' : pointer || '当前记录没有可预览正文。');
  return <aside className="th-trace-inspector" aria-label="Span 检查器">
    <header><div><span className={`th-trace-inspector-kind lane-${lane}`}>{traceRole(span)}</span><code>{turnNumber && stepNumber ? `Turn ${turnNumber} · Step ${stepNumber}` : `Span ${index + 1}`}</code></div><button type="button" aria-label="关闭 Span 检查器" onClick={onClose}>×</button></header>
    <nav aria-label="Span 详情标签" role="tablist">{tabs.map(([id,label], tabIndex) => <button key={id} type="button" role="tab" className={tab === id ? 'active' : ''} aria-selected={tab === id} tabIndex={tab === id ? 0 : -1} onClick={() => selectTab(id)} onKeyDown={event => {
      const next = event.key === 'ArrowRight' ? (tabIndex + 1) % tabs.length : event.key === 'ArrowLeft' ? (tabIndex + tabs.length - 1) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : null;
      if (next === null) return;
      event.preventDefault();
      selectTab(tabs[next][0]);
      event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus();
    }}>{label}</button>)}</nav>
    <div className="th-trace-inspector-body">
      {tab === 'summary' && <><dl>{lane === 'tools' ? <div><dt>Hierarchy</dt><dd>{parentSpan?.span_id && span.parent_id ? <button type="button" className="th-trace-source-link" onClick={() => onSelectSpan(span.parent_id as string)}>{hierarchyLabel} ›</button> : hierarchyLabel}</dd></div> : sourceLabel && <div><dt>Source</dt><dd><button type="button" className="th-trace-source-link" onClick={() => { setSourceExpanded(true); setTab(lane === 'input' ? 'source' : 'preview'); }}>{sourceLabel} ›</button></dd></div>}{rows.map(([label,value]) => <div key={label}><dt>{label}</dt><dd className={label === 'Status' && span.status === 'error' ? 'error' : ''}>{value}</dd></div>)}{usageRows.map(([label, value], rowIndex) => <div className={rowIndex ? 'th-trace-subrow' : undefined} key={`${label}:${rowIndex}`}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
        <details className="th-trace-inspector-section" key={`${span.span_id}:preview`} open><summary>Preview</summary>{reasoning != null && <details className="th-trace-thinking"><summary>Thinking</summary><TraceValue value={reasoning}/></details>}<p className="th-trace-preview-text">{previewText}</p></details>
        {lane === 'model' && timingRows.length > 0 && <details className="th-trace-inspector-section" key={`${span.span_id}:timing`} open><summary>Request Timing</summary><dl>{timingRows.map(([label,value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl></details>}
        {(span.parent_id || span.context_id || lane === 'tools' && sourceLabel) && <details className="th-trace-inspector-section"><summary>More</summary><dl>{span.parent_id && <div><dt>Parent Span</dt><dd>{span.parent_id}</dd></div>}{span.context_id && <div><dt>Context</dt><dd>{span.context_id}</dd></div>}{lane === 'tools' && sourceLabel && <div><dt>Source</dt><dd>{sourceLabel}</dd></div>}</dl></details>}</>}
      {tab === 'preview' && <div className="th-trace-preview-tab"><section><h4>{span.name || span.span_id || `Span ${index + 1}`}</h4><TraceValue value={previewValue} empty={responseId && !responsePreview.error ? '正在读取模型响应…' : '当前记录没有可预览正文。'}/></section>
        {requestId && <details onToggle={event => setRequestExpanded(event.currentTarget.open)}><summary>模型请求与响应</summary>{requestExpanded && <section>{visibleBody.loading && <p aria-live="polite">正在读取完整请求与响应…</p>}{visibleBody.error && <p role="alert">{visibleBody.error}</p>}{visibleBody.request !== undefined && <><h4>实际模型请求</h4><TraceValue value={visibleBody.request}/></>}{visibleBody.response !== undefined && <><h4>模型响应</h4><TraceValue value={visibleBody.response}/></>}</section>}</details>}
        {lane === 'model' && sourceRefs.length > 0 && <details open={sourceExpanded} onToggle={event => setSourceExpanded(event.currentTarget.open)}><summary>Source</summary><div className="th-trace-source-list">{sourceRefs.map((ref, sourceIndex) => <article key={`${ref.source_id || 'source'}:${ref.pointer}:${sourceIndex}`}><span>{ref.source_id || `Source ${sourceIndex + 1}`}</span><code>{ref.pointer}</code></article>)}</div></details>}
      </div>}
      {tab === 'raw' && <TraceValue value={raw ?? span}/>}
      {tab === 'source' && <div className="th-trace-preview-tab">{sourceRefs.length ? <div className="th-trace-source-list">{sourceRefs.map((ref, sourceIndex) => <article key={`${ref.source_id || 'source'}:${ref.pointer}:${sourceIndex}`}><span>{ref.source_id || `Source ${sourceIndex + 1}`}</span><code>{ref.pointer}</code></article>)}</div> : <p className="th-trace-inspector-empty">当前记录未提供来源引用。</p>}</div>}
      {tab === 'payload' && <div className="th-trace-preview-tab"><TraceValue value={input} empty="当前工具调用未记录 Payload。"/></div>}
      {tab === 'result' && <div className="th-trace-preview-tab"><TraceValue value={output} empty="当前工具调用未记录 Result。"/></div>}
      {tab === 'schema' && <div className="th-trace-preview-tab"><TraceValue value={schema} empty="Trace 原件未捕获该工具的 Schema。"/></div>}
      {tab === 'timing' && <div className="th-trace-inspector-tab-details">{timingRows.length ? <dl>{timingRows.map(([label,value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl> : <p className="th-trace-inspector-empty">当前记录未提供计时信息。</p>}</div>}
    </div>
  </aside>;
}

type TraceRangeDragMode = 'create' | 'move' | 'start' | 'end' | 'pan';

function TraceTimeline({ spans, selectedSpan, actualDuration, range, onSelect, onRangeChange }: {
  spans: SpanItem[];
  selectedSpan: string | null;
  actualDuration: boolean;
  range: TraceRange | null;
  onSelect: (spanId: string | null) => void;
  onRangeChange: (range: TraceRange | null) => void;
}) {
  const known = spans.filter(item => item.start_ms != null && item.end_ms != null);
  const useDuration = traceTimelineUsesDuration(spans, actualDuration);
  const sourceMin = useDuration ? Math.min(...known.map(item => item.start_ms as number)) : 0;
  const sourceMax = useDuration ? Math.max(...known.map(item => item.end_ms as number)) : Math.max(1, spans.length);
  const fullRange = Math.max(1, sourceMax - sourceMin);
  const [viewport, setViewport] = useState<TraceRange | null>(null);
  const [draft, setDraft] = useState<TraceRange | null>(null);
  const drag = useRef<{ pointerId: number; startX: number; startValue: number; mode: TraceRangeDragMode; origin: TraceRange } | null>(null);
  const trackRef = useRef<HTMLDivElement | null>(null);
  const plotRef = useRef<HTMLElement | null>(null);
  const domain = viewport || { start: sourceMin, end: sourceMax };
  const domainRange = Math.max(1, domain.end - domain.start);
  const bounds = { start: sourceMin, end: sourceMax };
  const minimumRange = Math.max(fullRange / Math.max(80, spans.length * 4), domainRange * .004);
  useEffect(() => { setViewport(null); setDraft(null); onRangeChange(null); }, [actualDuration, spans.length]);
  useEffect(() => {
    const track = trackRef.current;
    if (!track || !useDuration) return;
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = plotRef.current?.getBoundingClientRect() || track.getBoundingClientRect();
      const fraction = Math.max(0, Math.min(1, (event.clientX - rect.left) / Math.max(1, rect.width)));
      const nextRange = Math.max(fullRange / Math.max(8, spans.length), Math.min(fullRange, domainRange * Math.exp(event.deltaY * .0015)));
      if (nextRange >= fullRange * .995) { setViewport(null); return; }
      const anchor = domain.start + fraction * domainRange;
      const start = Math.max(sourceMin, Math.min(sourceMax - nextRange, anchor - fraction * nextRange));
      setViewport({ start, end: start + nextRange });
    };
    track.addEventListener('wheel', wheel, { passive: false });
    return () => track.removeEventListener('wheel', wheel);
  }, [domain.end, domain.start, domainRange, fullRange, sourceMax, sourceMin, spans.length, useDuration]);
  const lanes: Array<{ id: DshLane; label: string }> = [{ id: 'input', label: 'Input' }, { id: 'model', label: 'Model' }, { id: 'tools', label: 'Tools' }];
  const timeAt = (clientX: number) => {
    const rect = plotRef.current?.getBoundingClientRect();
    if (!rect) return domain.start;
    const fraction = Math.max(0, Math.min(1, (clientX - rect.left) / Math.max(1, rect.width)));
    return domain.start + fraction * domainRange;
  };
  const position = (span: SpanItem, index: number) => {
    if (!useDuration) {
      const unit = 100 / Math.max(1, spans.length);
      return { left: `${index * unit}%`, width: `${Math.max(.45, unit * .55)}%` };
    }
    const left = ((span.start_ms as number) - domain.start) / domainRange * 100;
    const right = ((span.end_ms as number) - domain.start) / domainRange * 100;
    return { left: `${left}%`, width: `${Math.max(.45, right - left)}%` };
  };
  const rangeDuringDrag = (current: NonNullable<typeof drag.current>, clientX: number) => {
    const value = timeAt(clientX);
    if (current.mode === 'create') return clampTraceRange({ start: current.startValue, end: value }, bounds);
    if (current.mode === 'move') return moveTraceRange(current.origin, value - current.startValue, bounds);
    if (current.mode === 'start' || current.mode === 'end') return resizeTraceRange(current.origin, current.mode, value, bounds, minimumRange);
    return null;
  };
  const endPointer = (event: ReactPointerEvent<HTMLDivElement>) => {
    const current = drag.current;
    if (!current || current.pointerId !== event.pointerId) return;
    const nextRange = rangeDuringDrag(current, event.clientX);
    drag.current = null;
    setDraft(null);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    if (current.mode === 'pan') return;
    if (current.mode === 'create' && Math.abs(event.clientX - current.startX) < 4) { onRangeChange(null); return; }
    if (nextRange && nextRange.end - nextRange.start >= minimumRange) onRangeChange(nextRange);
  };
  const activeRange = draft || range;
  const visibleRange = activeRange ? intersectTraceRange(activeRange, domain) : null;
  const rangeStyle = visibleRange ? { left: `${(visibleRange.start - domain.start) / domainRange * 100}%`, width: `${(visibleRange.end - visibleRange.start) / domainRange * 100}%` } : undefined;
  return <div className="th-dsh-lanes" aria-label="Trace 全局时间轴">
    <div ref={trackRef} className="th-dsh-timeline-track" tabIndex={0} onKeyDown={event => {
      if (event.key === 'Escape') { setDraft(null); onRangeChange(null); return; }
      if (!range || (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight')) return;
      const direction = event.key === 'ArrowLeft' ? -1 : 1;
      const step = domainRange * (event.shiftKey ? .1 : .01) * direction;
      const action = (event.target as HTMLElement).dataset.rangeAction;
      const nextRange = action === 'start' || action === 'end'
        ? resizeTraceRange(range, action, range[action] + step, bounds, minimumRange)
        : moveTraceRange(range, step, bounds);
      event.preventDefault();
      onRangeChange(nextRange);
    }} onContextMenu={event => event.preventDefault()} onDoubleClick={() => { setViewport(null); setDraft(null); onRangeChange(null); }} onPointerDown={event => {
      if (event.button !== 0 && event.button !== 2) return;
      const action = (event.target as HTMLElement).closest<HTMLElement>('[data-range-action]')?.dataset.rangeAction as TraceRangeDragMode | undefined;
      const mode: TraceRangeDragMode = event.button === 2 ? 'pan' : action || 'create';
      const origin = mode === 'pan' ? domain : range || { start: timeAt(event.clientX), end: timeAt(event.clientX) };
      if ((mode === 'move' || mode === 'start' || mode === 'end') && !range) return;
      drag.current = { pointerId: event.pointerId, startX: event.clientX, startValue: timeAt(event.clientX), mode, origin };
      event.currentTarget.setPointerCapture(event.pointerId);
      if (mode === 'create') setDraft({ start: timeAt(event.clientX), end: timeAt(event.clientX) });
      event.preventDefault();
    }} onPointerMove={event => {
      const current = drag.current;
      if (!current || current.pointerId !== event.pointerId) return;
      if (current.mode === 'pan' && useDuration && viewport) {
        const rect = plotRef.current?.getBoundingClientRect() || event.currentTarget.getBoundingClientRect();
        const delta = (event.clientX - current.startX) / Math.max(1, rect.width) * domainRange;
        const start = Math.max(sourceMin, Math.min(sourceMax - domainRange, current.origin.start - delta));
        setViewport({ start, end: start + domainRange });
      } else setDraft(rangeDuringDrag(current, event.clientX));
    }} onPointerUp={endPointer} onPointerCancel={() => { drag.current = null; setDraft(null); }}>
      {visibleRange && rangeStyle ? <div className="th-dsh-range-layer"><i className="th-dsh-range" style={rangeStyle}/><div className="th-dsh-range-controls" style={rangeStyle} aria-label={`已固定区间 ${useDuration ? compactDuration(activeRange!.end - activeRange!.start) : `${Math.max(1, Math.ceil(activeRange!.end) - Math.floor(activeRange!.start))} 条记录`}`}><button type="button" className="start" data-range-action="start" aria-label="调整区间起点"/><button type="button" className="move" data-range-action="move" aria-label="移动已固定区间"/><button type="button" className="end" data-range-action="end" aria-label="调整区间终点"/></div></div> : null}
      {lanes.map(lane => <div className={`th-dsh-lane lane-${lane.id}`} key={lane.id}><span>{lane.label}</span><i ref={lane.id === 'input' ? plotRef : undefined}>{spans.map((span, index) => dshLane(span) === lane.id ? <button type="button" key={`${span.span_id}:${index}`} className={`${span.status === 'error' ? 'error' : ''} ${selectedSpan === span.span_id ? 'active' : ''}`} style={position(span, index)} title={`${span.name || span.span_id || lane.label} · ${span.duration_ms == null ? '耗时未记录' : compactDuration(span.duration_ms)}`} disabled={!span.span_id} onPointerDown={event => event.stopPropagation()} onClick={() => onSelect(span.span_id)}/> : null)}</i></div>)}
    </div>
  </div>;
}

function rawSpanId(span: RawTraceSpan) { return String(span.id || span.span_id || ''); }

export function rawSpanItem(span: RawTraceSpan, index: number, runId: string, revision: number): SpanItem {
  const observed = span.timing as { start_ms?: unknown; end_ms?: unknown; duration_ms?: unknown; duration_basis?: unknown; duration_scope?: unknown } | null;
  const start = typeof observed?.start_ms === 'number' ? observed.start_ms : typeof span.start_ms === 'number' ? span.start_ms : null;
  const end = typeof observed?.end_ms === 'number' ? observed.end_ms : typeof span.end_ms === 'number' ? span.end_ms : null;
  const skill = (span.tool as { skill?: { name?: string | null; action?: string | null } } | undefined)?.skill || span.skill;
  const contextId = (span.model as { context_id?: string | null } | undefined)?.context_id || span.context_id;
  const sourceRefs = span.source_refs?.length ? span.source_refs : span.source ? [span.source] : null;
  const text = (value: unknown) => value == null || value === '' ? null : String(value);
  return {
    run_id: runId,
    revision,
    source_ordinal: typeof span.sequence === 'number' ? span.sequence : index,
    span_id: rawSpanId(span) || null,
    kind: text(span.kind),
    name: text(span.name),
    operation: text(span.operation),
    status: text(span.status),
    parent_id: text(span.parent_id),
    start_ms: start,
    end_ms: end,
    duration_ms: typeof observed?.duration_ms === 'number' ? observed.duration_ms : typeof span.duration_ms === 'number' ? span.duration_ms : start != null && end != null ? Math.max(0, end - start) : null,
    duration_basis: text(observed?.duration_basis) || text(span.duration_basis) || (start != null && end != null ? 'interval_derived' : null),
    duration_scope: text(observed?.duration_scope) || text(span.duration_scope),
    skill_name: text(skill?.name),
    skill_action: text(skill?.action),
    context_id: text(contextId),
    visibility_status: text(span.visibility_status),
    source_refs: sourceRefs,
  };
}

function spanMatches(span: SpanItem, raw: RawTraceSpan | undefined, query: string) {
  if (!query.trim()) return true;
  const needle = query.trim().toLocaleLowerCase();
  return [span.span_id, span.kind, span.name, span.operation, span.status, span.skill_name, span.skill_action, span.source_refs?.map(item => item.pointer).join(' '), readableValue(raw?.input), readableValue(raw?.output), readableValue(raw?.result)]
    .some(value => String(value || '').toLocaleLowerCase().includes(needle));
}

export function DshTraceAnalysis({ runId, spans, rawSpans = [], contexts = [], rawTurns = [], selectedSpan, onSelect, onLoadSource }: {
  runId: string;
  spans: SpanItem[];
  rawSpans?: RawTraceSpan[];
  contexts?: Array<Record<string, unknown>>;
  rawTurns?: Array<Record<string, unknown>>;
  selectedSpan: string | null;
  onSelect: (spanId: string | null) => void;
  onLoadSource?: (sourceId: string) => Promise<unknown>;
}) {
  const [actualDuration, setActualDuration] = useState(true);
  const [collapseTurns, setCollapseTurns] = useState(false);
  const [collapseCalls, setCollapseCalls] = useState(false);
  const [traceQuery, setTraceQuery] = useState('');
  const [timelineRange, setTimelineRange] = useState<TraceRange | null>(null);
  const eventsRef = useRef<HTMLDivElement | null>(null);
  const traceSearchRef = useRef<HTMLInputElement | null>(null);
  const rawById = useMemo(() => new Map(rawSpans.map(span => [rawSpanId(span), span])), [rawSpans]);
  const contextById = useMemo(() => new Map(contexts.map(context => [String(context.id), context])), [contexts]);
  const turnById = useMemo(() => new Map(rawTurns.map((turn, index) => [String(turn.id), index + 1])), [rawTurns]);
  const useDuration = traceTimelineUsesDuration(spans, actualDuration);
  const visibleSpans = useMemo(() => {
    const filtered = spans.filter((span, index) => {
      const raw = rawById.get(span.span_id || '');
      if (!spanMatches(span, raw, traceQuery)) return false;
      if (timelineRange && !traceSpanIntersectsRange(span, index, timelineRange, useDuration)) return false;
      return true;
    });
    const seenTurns = new Set<string>();
    const seenCalls = new Set<string>();
    return filtered.filter(span => {
      if (collapseTurns && span.context_id) {
        if (seenTurns.has(span.context_id)) return false;
        seenTurns.add(span.context_id);
      }
      if (collapseCalls && dshLane(span) === 'tools' && span.parent_id) {
        if (seenCalls.has(span.parent_id)) return false;
        seenCalls.add(span.parent_id);
      }
      return true;
    });
  }, [collapseCalls, collapseTurns, rawById, spans, timelineRange, traceQuery, useDuration]);
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === 'f') { event.preventDefault(); traceSearchRef.current?.focus(); return; }
      if (event.key === 'Escape' && selectedSpan) { onSelect(null); return; }
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
      const target = event.target as HTMLElement | null;
      if (target?.matches('input,textarea,[contenteditable="true"]')) return;
      const index = visibleSpans.findIndex(span => span.span_id === selectedSpan);
      const nextIndex = event.key === 'ArrowDown' ? Math.min(visibleSpans.length - 1, index + 1) : Math.max(0, index < 0 ? 0 : index - 1);
      const id = visibleSpans[nextIndex]?.span_id;
      if (!id) return;
      event.preventDefault();
      onSelect(id);
    };
    window.addEventListener('keydown', keydown);
    return () => window.removeEventListener('keydown', keydown);
  }, [onSelect, selectedSpan, visibleSpans]);
  useEffect(() => {
    if (!selectedSpan) return;
    const frame = window.requestAnimationFrame(() => {
      const events = eventsRef.current;
      if (!events) return;
      const target = Array.from(events.querySelectorAll<HTMLElement>('[data-span-id]')).find(element => element.dataset.spanId === selectedSpan);
      if (!target) return;
      const targetRect = target.getBoundingClientRect();
      if (targetRect.top < 0 || targetRect.bottom > window.innerHeight) target.scrollIntoView({ block: 'nearest', behavior: 'auto' });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [selectedSpan, spans.length]);
  const known = spans.filter(item => item.start_ms != null && item.end_ms != null);
  const min = known.length ? Math.min(...known.map(item => item.start_ms as number)) : 0;
  const max = known.length ? Math.max(...known.map(item => item.end_ms as number)) : min + 1;
  const range = known.length ? Math.max(0, max - min) : null;
  const contextTurns = new Set(spans.flatMap(item => item.context_id ? [item.context_id] : [])).size;
  const inputTurns = spans.filter(item => dshLane(item) === 'input').length;
  const turns = contextTurns || inputTurns || (spans.length ? 1 : 0);
  const activeSpanIndex = selectedSpan ? spans.findIndex(span => span.span_id === selectedSpan) : -1;
  const activeSpan = activeSpanIndex >= 0 ? spans[activeSpanIndex] : undefined;
  const activeRawSpan = activeSpan?.span_id ? rawById.get(activeSpan.span_id) : undefined;
  const activeOrder = activeRawSpan?.order as { sequence?: unknown } | undefined;
  return <section className="th-dsh-trace">
    <header className="th-dsh-trace-head"><div><strong>{runId}</strong><span>{spans.length} Spans</span></div><nav aria-label="Trace 概览"><span>Duration <b>{range == null ? '—' : compactDuration(range)}</b></span><span>Turns <b>{turns}</b></span><span>Calls <b>{spans.filter(item => dshLane(item) === 'tools').length}</b></span></nav></header>
    <div className="th-trace-toolbar" role="toolbar" aria-label="Trace 视图控制">
      <div><button type="button" className={actualDuration ? 'active' : ''} aria-pressed={actualDuration} onClick={() => setActualDuration(value => !value)}>◷ Duration</button><button type="button" className={collapseTurns ? 'active' : ''} aria-pressed={collapseTurns} onClick={() => setCollapseTurns(value => !value)}>⊟ Turns</button><button type="button" className={collapseCalls ? 'active' : ''} aria-pressed={collapseCalls} onClick={() => setCollapseCalls(value => !value)}>⊟ Calls</button></div>
      <label><span aria-hidden="true">⌕</span><input ref={traceSearchRef} type="search" value={traceQuery} onChange={event => setTraceQuery(event.currentTarget.value)} placeholder="搜索记录、内容或 ID" aria-label="搜索 Trace 记录"/><kbd>⌘ F</kbd></label>
    </div>
    <TraceTimeline spans={spans} selectedSpan={selectedSpan} actualDuration={actualDuration} range={timelineRange} onSelect={onSelect} onRangeChange={setTimelineRange}/>
    <div className={`th-dsh-trace-body ${activeSpan ? 'inspector-open' : ''}`}>
      <div ref={eventsRef} className="th-dsh-events" aria-label="Trace 事件流">{visibleSpans.map((span, index) => { const lane = dshLane(span); const raw = rawById.get(span.span_id || ''); const pointer = span.source_refs?.[0]?.pointer || raw?.source?.pointer; const summary = compactPreview(raw?.output ?? raw?.input); return <button type="button" id={span.span_id || undefined} data-span-id={span.span_id || undefined} className={`${selectedSpan === span.span_id ? 'active' : ''} lane-${lane}`} key={`${span.span_id}:${index}`} disabled={!span.span_id} onClick={() => onSelect(span.span_id)}><i aria-hidden="true"/><span className="th-dsh-event-role">{traceRole(span)}</span><span className="th-dsh-event-copy"><strong>{span.name || span.span_id || `Span ${index + 1}`}</strong><span>{summary || pointer || span.operation || span.kind || '无来源信息'}</span></span><time>{span.duration_ms == null ? '—' : compactDuration(span.duration_ms)}</time>{span.status === 'error' && <em>失败</em>}</button>; })}{!visibleSpans.length && <div className="th-trace-no-results">没有符合当前搜索或时间范围的记录</div>}</div>
      {activeSpan && <TraceSpanInspector span={activeSpan} raw={activeRawSpan} context={contextById.get(String((activeRawSpan?.model as {context_id?: string} | undefined)?.context_id || ''))} parentSpan={spans.find(item => item.span_id === activeSpan.parent_id)} index={activeSpanIndex} turnNumber={typeof activeRawSpan?.turn_id === 'string' ? turnById.get(activeRawSpan.turn_id) : undefined} stepNumber={typeof activeOrder?.sequence === 'number' ? activeOrder.sequence + 1 : undefined} onClose={() => onSelect(null)} onSelectSpan={spanId => onSelect(spanId)} onLoadSource={onLoadSource}/>}
    </div>
  </section>;
}

export function InfraTracePage({ runId }: { runId?: string }) {
  const location = useLocation();
  const navigate = useNavigate();
  const { projects, project: storedProject, setProject, projectError, refreshProjects } = useInfraProject();
  const params = new URLSearchParams(location.search);
  const project = params.get('project') || (runId ? storedProject : traceProjectSelection(projects, storedProject));
  const projectOptions = useMemo(() => traceProjectOptions(projects), [projects]);
  const agentProjectIds = useMemo(() => new Set(agentTraceProjects(projects).map(item => item.project_id)), [projects]);
  const selectedProject = traceProjectSelection(projects, project);
  const selectedRevision = Number(params.get('revision') || 1);
  const loadTraceSource = useCallback((sourceId: string) => infraApi.agentTraceSource(project, runId || '', selectedRevision, sourceId), [project, runId, selectedRevision]);
  const selectProject = (value: string) => {
    if (value !== agentTraceGroupId) setProject(value);
    navigate(`/traces?project=${encodeURIComponent(value)}`);
  };
  const selectedSpan = params.get('span');
  const [traces, setTraces] = useState<ProjectTraceItem[]>([]);
  const [spans, setSpans] = useState<SpanItem[]>([]);
  const [rawTrace, setRawTrace] = useState<RawTraceDocument>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(true);
  const [traceIdQuery, setTraceIdQuery] = useState('');
  const forceRefresh = useRef(false);
  const [refreshToken, setRefreshTokenState] = useState(0);
  const setRefreshToken = (update: (value: number) => number) => {
    forceRefresh.current = true;
    setRefreshTokenState(update);
  };
  const visibleTraces = useMemo(() => {
    const query = traceIdQuery.trim().toLocaleLowerCase();
    if (!query) return traces;
    return traces.filter(item => [item.display_title, item.run_id, item.query_id].some(value => String(value || '').toLocaleLowerCase().includes(query)));
  }, [traceIdQuery, traces]);
  useEffect(() => setTraceIdQuery(''), [project]);
  useEffect(() => {
    if (!project) return;
    if (!runId && project === agentTraceGroupId && !projects.length) { setBusy(true); return; }
    let active = true;
    setError('');
    if (runId) {
      const key = cacheKey('trace-content', project, runId, selectedRevision);
      const cached = peekCachedQuery<RawTraceDocument>(key);
      if (cached) {
        setRawTrace(cached);
        setSpans((cached.spans || []).map((span, index) => rawSpanItem(span, index, runId, selectedRevision)));
      }
      setBusy(!cached);
      loadCachedQuery(key, () => infraApi.traceContent(project, runId, selectedRevision), { ttlMs: 86_400_000 })
        .then(document => {
          if (!active) return;
          setRawTrace(document);
          setSpans((document.spans || []).map((span, index) => rawSpanItem(span, index, runId, selectedRevision)));
        })
        .catch(reason => { if (active) setError(reason.message); })
        .finally(() => { if (active) setBusy(false); });
    } else {
      const cached = project === agentTraceGroupId ? undefined : peekCachedQuery<TraceItem[]>(cacheKey('trace-list', project));
      if (cached) setTraces(mergeProjectTraces([{ project_id: project, items: cached }])); else setTraces([]);
      setBusy(!cached);
      const force = forceRefresh.current;
      forceRefresh.current = false;
      const load = project === agentTraceGroupId
        ? allAgentTraces(projects, force)
        : allTraces(project, force).then(items => {
          const rows = mergeProjectTraces([{ project_id: project, items }]);
          return agentProjectIds.has(project) ? loadAgentTraceLabels(rows) : rows;
        });
      load.then(value => { if (active) setTraces(value); })
        .catch(reason => { if (active) setError(reason.message); })
        .finally(() => { if (active) setBusy(false); });
    }
    return () => { active = false; };
  }, [project, runId, selectedRevision, refreshToken, projects, agentProjectIds]);
  if (runId) {
    const focusSpan = (spanId: string | null) => {
      const next = new URLSearchParams(location.search);
      if (spanId) next.set('span', spanId); else next.delete('span');
      navigate(`${location.pathname}?${next.toString()}`, { replace: true });
    };
    return <div className="th-infra-page"><ProjectBar projects={projects} project={selectedProject} setProject={selectProject} options={projectOptions}><Link className="th-quiet-link" to={`/traces?project=${encodeURIComponent(selectedProject)}`}>返回轨迹库</Link></ProjectBar><ErrorNotice value={projectError || error}/>{busy ? <InfraBusy/> : <DshTraceAnalysis runId={runId} spans={spans} rawSpans={rawTrace?.spans} contexts={rawTrace?.contexts} rawTurns={rawTrace?.turns} selectedSpan={selectedSpan} onSelect={focusSpan} onLoadSource={loadTraceSource}/>}</div>;
  }
  return <div className="th-infra-page"><ProjectBar projects={projects} project={selectedProject} setProject={selectProject} options={projectOptions}><label className="th-trace-id-search"><Icon name="search" size={17}/><input type="search" value={traceIdQuery} onChange={event => setTraceIdQuery(event.currentTarget.value)} placeholder="搜索提问或 Trace ID" aria-label="搜索提问或 Trace ID"/></label><button disabled={busy} onClick={() => { if (project === agentTraceGroupId) { forceRefresh.current = true; void refreshProjects(true).catch(reason => setError(reason.message)); } else setRefreshToken(value => value + 1); }}>{busy && traces.length ? '刷新中' : '刷新'}</button></ProjectBar><ErrorNotice value={projectError || error}/>{busy && !traces.length ? <InfraBusy/> : visibleTraces.length ? <section className="th-real-trace-list">{visibleTraces.map(item => {
    const ownTrace = agentProjectIds.has(item.project_id);
    const created = item.created_at ? new Date(item.created_at) : null;
    const timestamp = created && Number.isFinite(created.getTime()) ? created.toLocaleString('zh-CN', { hour12: false }) : '';
    return <Link key={`${item.project_id}:${item.run_id}:${item.revision}`} to={`/traces/${encodeURIComponent(item.run_id || '')}?project=${encodeURIComponent(item.project_id)}&revision=${item.revision}`}><div><strong title={ownTrace ? item.display_title || undefined : undefined}>{ownTrace ? item.display_title || 'Agent 会话' : item.run_id}</strong>{ownTrace ? <span className="th-trace-list-caption">Trace Hunter Agent{timestamp ? ` · ${timestamp}` : ''}</span> : <code>{item.query_id || item.format_version}</code>}</div><span className={`th-index-state ${item.index_state}`}>{item.index_state}</span><span>{item.record_count ?? '—'} spans</span><span>{item.tool_count ?? '—'} tools</span><Icon name="chevron" size={14}/></Link>;
  })}</section> : <div className="th-infra-empty"><strong>{traceIdQuery.trim() ? '没有匹配的提问或 Trace ID' : '当前项目没有 Trace'}</strong></div>}</div>;
}

function InfraBusy() { return <div className="th-infra-busy"><i/><span>读取真实数据</span></div>; }

type BatchImportState = 'queued' | 'running' | 'succeeded' | 'failed' | 'duplicate';
type AutoUploadStatus = UploadStatus & { agent_session_id?: string | null; agent_turn_id?: string | null; native_terminal_id?: string | null };
type BatchImportItem = {
  id: string;
  project: string;
  fileName: string;
  fileSize: number;
  state: BatchImportState;
  job?: InfraTask;
  upload?: AutoUploadStatus;
  uploadId?: string;
  taskId?: string;
  error: string;
  duplicateImportState?: DuplicateImportState;
  startedAt: number | null;
  completedAt: number | null;
};

function importError(job?: InfraTask) {
  if (!job?.error) return '';
  return typeof job.error === 'string' ? job.error : job.error.message || job.error.code || '导入失败';
}

function importStatus(item: BatchImportItem) {
  if (item.state === 'duplicate') return duplicateImportNotice(item.duplicateImportState || 'submitted');
  if (item.state === 'succeeded') return '导入成功：Agent 已完成处理，Trace 已回读验证';
  if (item.state === 'failed') return '导入未成功；请查看错误或重试';
  if (item.state === 'queued') return '等待上传；尚未导入';
  if (!item.upload || item.upload.progress < 1) return '正在上传文件；尚未导入';
  const stage = item.job?.current_stage ? (importStepLabels as Record<string, string>)[item.job.current_stage] : null;
  return stage ? `上传完成；Agent 处理阶段：${stage}。尚未导入成功` : '上传完成；等待 Agent 开始处理。尚未导入成功';
}

function importRequestId() {
  const bytes = new Uint8Array(16);
  if (globalThis.crypto?.getRandomValues) globalThis.crypto.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const value = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${value.slice(0, 8)}-${value.slice(8, 12)}-${value.slice(12, 16)}-${value.slice(16, 20)}-${value.slice(20)}`;
}

function adaptiveImportConcurrency() {
  if (typeof navigator === 'undefined') return 8;
  const cores = Math.max(1, navigator.hardwareConcurrency || 8);
  const memory = Math.max(1, Number((navigator as Navigator & { deviceMemory?: number }).deviceMemory || 8));
  return Math.max(8, Math.min(24, Math.floor(cores * 0.75), Math.floor(memory * 2)));
}

export function InfraImportPage() {
  const location = useLocation();
  const { projects, project, setProject, projectError } = useInfraProject();
  const [items, setItems] = useState<BatchImportItem[]>([]);
  const [active, setActive] = useState(false);
  const concurrency = Math.min(4, adaptiveImportConcurrency());
  const [batchStartedAt, setBatchStartedAt] = useState<number | null>(null);
  const [batchFinishedAt, setBatchFinishedAt] = useState<number | null>(null);
  const [error, setError] = useState('');
  const batchAbort = useRef<AbortController | null>(null);
  const busy = active || items.some(item => item.state === 'queued' || item.state === 'running');
  useEffect(() => {
    const saved = localStorage.getItem(`trace-hunter-auto-import:${project}`);
    try {
      const parsed: unknown = saved ? JSON.parse(saved) : [];
      setItems(Array.isArray(parsed) ? parsed.filter((item): item is BatchImportItem =>
        Boolean(item && typeof item === 'object' && item.project === project && typeof item.id === 'string')) : []);
    } catch { setItems([]); }
  }, [project]);
  useEffect(() => () => batchAbort.current?.abort(), []);
  function updateItem(id: string, update: (value: BatchImportItem) => BatchImportItem) {
    setItems(current => {
      const next = current.map(item => item.id === id ? update(item) : item);
      localStorage.setItem(`trace-hunter-auto-import:${project}`, JSON.stringify(next.map(item => ({
        id: item.id, project: item.project, fileName: item.fileName, fileSize: item.fileSize,
        state: item.state, error: item.error, duplicateImportState: item.duplicateImportState,
        startedAt: item.startedAt,
        completedAt: item.completedAt, uploadId: item.uploadId, taskId: item.taskId,
      }))));
      return next;
    });
  }
  async function importFile(file: File, id: string, signal: AbortSignal) {
    updateItem(id, item => ({ ...item, state: 'running', startedAt: Date.now() }));
    try {
      const agent = await infraApi.agentCapabilities(signal);
      if (!agent.enabled) throw new Error('当前服务尚未启用 Agent 导入');
      const raw = await file.arrayBuffer();
      if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
      const digest = await sha256Hex(raw);
      const nameBuffer = new TextEncoder().encode(file.name);
      const nameDigest = await sha256Hex(nameBuffer.buffer);
      let upload = await infraApi.createUpload(project, {
        request_key: `web-auto:${project}:${digest}:${nameDigest}`,
        source_format: 'auto', size_bytes: file.size, sha256: digest,
        part_size: 8 * 1024 * 1024, expected_previous: 0,
        binding: {}, source_name: file.name,
      }, signal);
      updateItem(id, item => ({ ...item, upload, uploadId: upload.upload_id }));
      if (upload.state !== 'completed') {
        upload = await infraApi.uploadStatus(project, upload.upload_id, signal);
        updateItem(id, item => ({ ...item, upload }));
        for (const position of upload.missing_parts) {
          const start = position * upload.part_size;
          const chunk = file.slice(start, Math.min(file.size, start + upload.part_size));
          const chunkBuffer = await chunk.arrayBuffer();
          upload = await infraApi.uploadPart(project, upload.upload_id, position, chunk, await sha256Hex(chunkBuffer), signal);
          updateItem(id, item => ({ ...item, upload }));
        }
        upload = await infraApi.completeUpload(project, upload.upload_id, signal);
      }
      if (!upload.job_id) throw new Error('上传完成但没有创建 Agent 导入任务');
      const taskId = upload.job_id;
      const job = await infraApi.taskStatus(project, taskId, signal);
      const alreadyImported = reusedSuccessfulImport(upload.reused, job.state);
      updateItem(id, item => ({ ...item, upload, uploadId: upload.upload_id,
        taskId, job, state: alreadyImported ? 'duplicate' :
          job.state === 'failed' || job.state === 'cancelled' ? 'failed' :
            job.state === 'succeeded' ? 'succeeded' : 'running',
        duplicateImportState: alreadyImported ? 'imported' : undefined, error: importError(job) }));
    } catch (reason) {
      if ((reason as Error).name === 'AbortError') return;
      const duplicateState = duplicateImportFromCode((reason as Error & { code?: string }).code);
      if (duplicateState) {
        updateItem(id, item => ({ ...item, state: 'duplicate',
          duplicateImportState: duplicateState,
          error: '', completedAt: Date.now() }));
        return;
      }
      updateItem(id, item => ({ ...item, state: 'failed', error: (reason as Error).message, completedAt: Date.now() }));
    }
  }
  async function upload(files: File[]) {
    if (!files.length || busy) return;
    const pending: BatchImportItem[] = files.map(file => ({ id: importRequestId(), project,
      fileName: file.name, fileSize: file.size, state: 'queued', error: '', startedAt: null,
      completedAt: null }));
    const ctrl = new AbortController();
    batchAbort.current?.abort(); batchAbort.current = ctrl;
    setItems(pending); setError(''); setActive(true); setBatchStartedAt(Date.now()); setBatchFinishedAt(null);
    localStorage.setItem(`trace-hunter-auto-import:${project}`, JSON.stringify(pending));
    let cursor = 0;
    async function worker() {
      while (!ctrl.signal.aborted) {
        const index = cursor++;
        if (index >= files.length) return;
        await importFile(files[index], pending[index].id, ctrl.signal);
      }
    }
    await Promise.all(Array.from({ length: Math.min(concurrency, files.length) }, worker));
    if (!ctrl.signal.aborted) { setBatchFinishedAt(Date.now()); setActive(false); }
  }
  const pollingKey = items.map(item => `${item.id}:${item.taskId || ''}:${item.state}:${item.job ? 'loaded' : 'pending'}:${item.upload ? 'upload' : 'restore'}`).join('|');
  useEffect(() => {
    let stopped = false;
    let inFlight = false;
    const poll = async () => {
      if (inFlight || stopped) return;
      inFlight = true;
      try {
        await Promise.all(items.filter(item => item.uploadId && !item.upload).map(async item => {
          try {
            const upload = await infraApi.uploadStatus(project, item.uploadId!);
            if (!stopped) updateItem(item.id, value => ({ ...value, upload,
              state: value.taskId ? value.state : 'failed',
              error: value.taskId ? value.error : '请选择同一文件继续上传' }));
          } catch (reason) {
            if (!stopped) updateItem(item.id, value => ({ ...value, state: 'failed', error: (reason as Error).message }));
          }
        }));
        await Promise.all(items.filter(item => item.taskId && (item.state === 'running' || !item.job)).map(async item => {
          try {
            const [job, upload] = await Promise.all([
              infraApi.taskStatus(project, item.taskId!),
              item.uploadId ? infraApi.uploadStatus(project, item.uploadId).catch(() => null) : Promise.resolve(null),
            ]);
            if (stopped) return;
            const state = job.state === 'failed' || job.state === 'cancelled' ? 'failed' :
              job.state === 'succeeded' ? 'succeeded' : 'running';
            if (state === 'succeeded') invalidateProjectCache(project);
            updateItem(item.id, value => ({ ...value, job, upload: upload || value.upload,
              state: value.duplicateImportState === 'imported' && state === 'succeeded' ? 'duplicate' : state,
              error: importError(job),
              completedAt: state === 'running' ? null : Date.now() }));
          } catch (reason) {
            if (!stopped) updateItem(item.id, value => ({ ...value, error: (reason as Error).message }));
          }
        }));
      } finally { inFlight = false; }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 1500);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [project, pollingKey]);
  async function retry(item: BatchImportItem) {
    if (!item.taskId || !item.uploadId) return;
    try {
      const job = await infraApi.retryTask(project, item.taskId);
      const upload = await infraApi.uploadStatus(project, item.uploadId);
      updateItem(item.id, value => ({ ...value, upload, job, taskId: job.task_id,
        state: 'running', duplicateImportState: undefined, error: '', completedAt: null }));
    } catch (reason) { updateItem(item.id, value => ({ ...value, error: (reason as Error).message })); }
  }
  async function cancel(item: BatchImportItem) {
    if (!item.taskId) return;
    try {
      const job = await infraApi.cancelTask(project, item.taskId);
      updateItem(item.id, value => ({ ...value, job, state: 'failed', error: '已取消', completedAt: Date.now() }));
    } catch (reason) { updateItem(item.id, value => ({ ...value, error: (reason as Error).message })); }
  }
  const supportedSteps = Object.entries(importStepLabels).map(([id, label]) => ({ id, label }));
  const succeeded = items.filter(item => item.state === 'succeeded').length;
  const failed = items.filter(item => item.state === 'failed').length;
  const duplicates = items.filter(item => item.state === 'duplicate').length;
  const running = items.filter(item => item.state === 'running').length;
  const queued = items.filter(item => item.state === 'queued').length;
  const totalUnits = items.length * (supportedSteps.length + 1);
  const completedUnits = items.reduce((sum, item) => {
    if (item.state === 'succeeded' || item.state === 'failed' || item.state === 'duplicate')
      return sum + supportedSteps.length + 1;
    const uploadProgress = item.upload?.progress ?? (item.job ? 1 : 0);
    return sum + uploadProgress + (item.job?.progress || 0) * supportedSteps.length;
  }, 0);
  const percent = totalUnits ? Math.round(completedUnits / totalUnits * 100) : 0;
  const now = batchFinishedAt || Date.now();
  const elapsedSeconds = batchStartedAt ? Math.max((now - batchStartedAt) / 1000, 0.001) : 0;
  const throughput = elapsedSeconds ? succeeded / elapsedSeconds : 0;
  const etaSeconds = completedUnits > 0 && totalUnits > completedUnits ? elapsedSeconds / completedUnits * (totalUnits - completedUnits) : 0;
  const singleResult = items.length === 1 && items[0]?.state === 'succeeded'
    ? items[0]?.job?.result as { run_id?: string; revision?: number; adapter?: string } | null : null;
  return <div className="th-infra-page th-simple-import"><ProjectBar projects={projects} project={project} setProject={setProject}><span className="th-import-auto">Agent 自动识别格式 · 同时上传 {concurrency} 个文件</span><label className={`th-file-action ${busy ? 'disabled' : ''}`}><Icon name="upload" size={16}/>{busy ? '处理中' : '选择 Trace 文件'}<input disabled={busy} multiple type="file" accept=".json,.jsonl,.ndjson,.csv,application/json" onChange={event => { const files = Array.from(event.target.files || []); event.currentTarget.value = ''; if (files.length) void upload(files); }}/></label></ProjectBar><ErrorNotice value={projectError || error}/>{items.length > 0 && <>
    <section className="th-batch-summary"><header><strong>{items.length} 个 Trace 文件</strong><b>{percent}%</b></header><progress value={percent} max="100"/><p className="th-import-progress-note">总进度包含上传、Agent 处理和结果回读；只有回读完成才算导入成功。{duplicates ? `其中 ${duplicates} 个文件已有记录，不计入新导入成功或失败。` : null}</p><div><Metric label="Queued" value={String(queued)}/><Metric label="Running" value={String(running)}/><Metric label="Succeeded" value={String(succeeded)}/><Metric label="Failed" value={String(failed)}/><Metric label="吞吐" value={`${throughput.toFixed(2)} traces/s`}/><Metric label="ETA" value={active && etaSeconds ? `${Math.ceil(etaSeconds)}s` : running ? 'Agent 处理中' : '—'}/></div></section>
    <section className="th-batch-files">{items.map(item => {
      const steps = item.job?.steps || supportedSteps.map(step => ({ ...step, state: 'pending' as const }));
      const uploadPercent = Math.round((item.upload?.progress ?? (item.job ? 1 : 0)) * 100);
      const result = item.job?.result as { run_id?: string; revision?: number; adapter?: string } | null;
      const sessionId = item.upload?.native_terminal_id;
      const params = new URLSearchParams(location.search);
      if (sessionId) params.set('agent_session', sessionId);
      return <article key={item.id} className={item.state}>
        <header><div><strong>{item.fileName}</strong><span>{(item.fileSize / 1024).toFixed(1)} KB</span></div>
          {(sessionId || result?.run_id) && <div className="th-import-actions">
            {sessionId && <Link className="th-import-action" to={{ pathname: location.pathname, search: params.toString() }} aria-label={`打开 ${item.fileName} 的原生 Claude 会话`}>打开会话</Link>}
            {result?.run_id && <Link className="th-import-action" to={`/traces/${encodeURIComponent(result.run_id)}?project=${encodeURIComponent(project)}&revision=${result.revision || 1}`}>{item.state === 'duplicate' ? '查看已导入的 Trace' : '查看导入的 Trace'}</Link>}
          </div>}
          <b>{item.state === 'duplicate' ? item.duplicateImportState === 'imported' ? '已导入' : '已有记录'
            : item.state === 'succeeded' ? '成功' : item.state === 'failed' ? '失败'
              : item.state === 'queued' ? '排队中' : '处理中'}</b></header>
        <p className="th-import-current-state" role="status">{importStatus(item)}</p>
        {item.state !== 'duplicate' && <>
          <div className="th-upload-stage"><span>上传{item.upload ? ' · 可断点续传' : ''}</span><b>{uploadPercent}%</b><progress value={uploadPercent} max="100"/></div>
          <div className="th-batch-file-steps">{steps.map((step, indexValue) => <span className={step.state || 'pending'} key={step.id}><i>{step.state === 'completed' ? '✓' : indexValue + 1}</i>{step.label || (importStepLabels as Record<string, string>)[step.id || '']}</span>)}</div>
        </>}
        {item.state === 'duplicate' && <p><Link to={`/traces?project=${encodeURIComponent(project)}`}>查看轨迹库</Link></p>}
        {result?.run_id && <p>Adapter：{result.adapter || '已选择'}</p>}
        {item.error && <p className="th-import-error" role="alert">{item.error}</p>}
        {item.state === 'running' && item.taskId && <button type="button" onClick={() => void cancel(item)}>取消</button>}
        {item.state === 'failed' && item.uploadId && <button type="button" onClick={() => void retry(item)}>重试</button>}
      </article>;
    })}</section>
    {singleResult?.run_id && <section className="th-import-summary"><Metric label="Run" value={singleResult.run_id}/><Metric label="Revision" value={String(singleResult.revision || 1)}/><Metric label="Adapter" value={singleResult.adapter || '已选择'}/></section>}</>}
  </div>;
}

export function InfraTasksPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const params = new URLSearchParams(location.search);
  const targetProject = params.get('project') || '';
  const targetTaskId = params.get('task') || '';
  const { projects, project, setProject, projectError } = useInfraProject(false, targetProject);
  type CachedTasks = { tasks: InfraTask[]; batches: Map<string, ImportBatch>; truncated: boolean; updatedAt: string };
  const initialProject = targetProject || localStorage.getItem('trace-hunter-infra-project') || '';
  const initial = !targetTaskId && initialProject ? peekCachedQuery<CachedTasks>(cacheKey('tasks', initialProject)) : undefined;
  const [tasks, setTasks] = useState<InfraTask[]>(initial?.tasks || []);
  const [importBatches, setImportBatches] = useState(initial?.batches || new Map<string, ImportBatch>());
  const [truncated, setTruncated] = useState(initial?.truncated || false);
  const [loading, setLoading] = useState(!initial);
  const [updatedAt, setUpdatedAt] = useState(initial?.updatedAt || '');
  const [cancelling, setCancelling] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    if (targetProject && project !== targetProject && projects.some(item => item.project_id === targetProject)) {
      setProject(targetProject);
    }
  }, [targetProject, project, projects]);
  useEffect(() => {
    if (!project) { setTasks([]); setLoading(false); return; }
    const cached = peekCachedQuery<CachedTasks>(cacheKey('tasks', project));
    if (cached) {
      setTasks(cached.tasks); setImportBatches(cached.batches); setTruncated(cached.truncated);
      setUpdatedAt(cached.updatedAt); setLoading(false);
    } else {
      setTasks([]); setImportBatches(new Map()); setTruncated(false); setLoading(true);
    }
    const ctrl = new AbortController();
    let inFlight = false;
    const sync = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        if (targetTaskId) {
          const task = await infraApi.taskStatus(project, targetTaskId, ctrl.signal);
          const linked = linkTasksToAgentSessions([task], await loadTaskAgentSessions())[0];
          if (!ctrl.signal.aborted) {
            setTasks([linked]); setImportBatches(new Map()); setTruncated(false);
            setUpdatedAt(new Date().toISOString()); setError('');
          }
          return;
        }
        const value = await infraApi.tasks(project, { limit: 200 }, ctrl.signal);
        const linkedTasks = linkTasksToAgentSessions(value.items, await loadTaskAgentSessions());
        const batchIds = Array.from(new Set(value.items.map(taskBatchId).filter(Boolean)));
        const summaries = await Promise.all(batchIds.map(async batchId => {
          try { return [batchId, await infraApi.importBatch(project, batchId, { limit: 1 }, ctrl.signal)] as const; }
          catch { return null; }
        }));
        const batches = new Map(summaries.filter((value): value is readonly [string, ImportBatch] => Boolean(value)));
        const nextUpdatedAt = new Date().toISOString();
        setTasks(linkedTasks); setImportBatches(batches); setTruncated(value.truncated); setUpdatedAt(nextUpdatedAt); setError('');
        setCachedQuery(cacheKey('tasks', project), { tasks: linkedTasks, batches, truncated: value.truncated, updatedAt: nextUpdatedAt });
      } catch (reason) {
        if (!ctrl.signal.aborted) setError((reason as Error).message);
      } finally {
        inFlight = false;
        if (!ctrl.signal.aborted) setLoading(false);
      }
    };
    void sync();
    const timer = window.setInterval(() => void sync(), 1000);
    return () => { ctrl.abort(); window.clearInterval(timer); };
  }, [project, targetTaskId]);
  async function cancel(taskIds: string[]) {
    if (!project || cancelling) return;
    const ctrl = new AbortController(); setCancelling(taskIds.join(',')); setError('');
    try {
      const values = await Promise.all(taskIds.map(taskId => infraApi.cancelTask(project, taskId, ctrl.signal)));
      const updates = new Map(values.map(value => [value.task_id, value]));
      setTasks(current => current.map(item => updates.get(item.task_id) || item));
    } catch (reason) { setError((reason as Error).message); }
    finally { setCancelling(''); }
  }
  const groups = useMemo(() => groupInfraTasks(tasks), [tasks]);
  const active = groups.filter(task => task.state === 'queued' || task.state === 'running').length;
  const failed = groups.filter(task => task.state === 'failed').length;
  const succeeded = groups.filter(task => task.state === 'succeeded').length;
  const selectProject = (value: string) => {
    setProject(value);
    if (targetTaskId) navigate(`/tasks?project=${encodeURIComponent(value)}`);
  };
  return <div className="th-infra-page th-task-center"><ProjectBar projects={projects} project={project} setProject={selectProject}><span className="th-live-source">每秒同步{updatedAt ? ` · ${formatTaskTime(updatedAt)}` : ''}</span></ProjectBar><ErrorNotice value={projectError || error}/>{targetTaskId && <Link className="th-quiet-link" to={`/tasks?project=${encodeURIComponent(project)}`}>返回全部任务</Link>}{loading && !tasks.length ? <InfraBusy/> : error && !tasks.length ? null : <><section className="th-task-summary"><Metric label={targetTaskId ? '当前任务' : '当前批次'} value={String(groups.length)}/><Metric label="进行中" value={String(active)}/><Metric label="已完成" value={String(succeeded)}/><Metric label="失败" value={String(failed)}/></section><TaskList groups={groups} batches={importBatches} limited={truncated} cancelling={cancelling} onCancel={taskIds => void cancel(taskIds)}/></>}</div>;
}

const taskStateLabels: Record<InfraTask['state'], string> = { queued: '排队中', running: '进行中', succeeded: '已完成', failed: '失败', cancelled: '已取消' };
const taskKindLabels: Record<InfraTask['kind'], string> = { adapter_import: '导入', evaluation: '评测', analysis: '分析', custom: '自定义' };
const taskStageLabels: Record<string, string> = { ...importStepLabels, execute: 'Agent 执行', capture: 'Trace 归档' };
function formatTaskTime(value: string | null) { if (!value) return '—'; const date = new Date(value); return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(date); }
function taskPercent(task: InfraTaskGroup) { return Math.round(Math.max(0, Math.min(1, task.progress)) * 100); }

function TaskList({ groups, batches, limited, cancelling, onCancel }: { groups: InfraTaskGroup[]; batches: Map<string, ImportBatch>; limited: boolean; cancelling: string; onCancel: (taskIds: string[]) => void }) {
  const location = useLocation();
  if (!groups.length) return <section className="th-task-empty"><strong>暂无任务</strong></section>;
  return <section className="th-task-list">{groups.map(task => { const batch = task.isBatch ? batches.get(task.title) : undefined; const percent = batch ? Math.round(batch.progress * 100) : taskPercent(task); const itemCount = batch?.total ?? task.itemCount; const completedCount = batch?.completed ?? task.completedCount; const partial = task.isBatch && limited && !batch; const failure = task.state === 'failed' ? task.errors[0] || '未记录具体错误' : ''; const cancellable = task.activeTaskIds.length > 0; return <article className={`state-${task.state}`} key={task.id}>
    <header><div><span>{task.isBatch ? `${taskKindLabels[task.kind]}批次` : taskKindLabels[task.kind] || task.kind}</span><strong>{task.title}</strong></div><div>
      {task.agentSession && <Link className="th-task-agent-link" to={taskAgentHref(location.pathname, location.search, task.agentSession)}>打开 Agent 会话</Link>}
      {task.agentUnlinked && <span className="th-task-agent-unlinked">无可打开的会话</span>}
      <b>{taskStateLabels[task.state]}</b>{cancellable && <button disabled={Boolean(cancelling)} onClick={() => onCancel(task.activeTaskIds)}>{cancelling ? '取消中' : task.isBatch ? '取消批次' : '取消任务'}</button>}</div></header>
    <div className="th-task-stage"><strong>{task.currentStage ? taskStageLabels[task.currentStage] || task.currentStage : task.isBatch ? partial ? `最近 ${task.itemCount} 个文件，已完成 ${task.completedCount}` : `已完成 ${completedCount} / ${itemCount} 个文件` : taskStateLabels[task.state]}</strong><b>{percent}%</b></div>
    <progress value={percent} max="100" />
    <dl><div><dt>{task.isBatch ? partial ? '最近记录' : '文件' : '任务'}</dt><dd>{itemCount.toLocaleString()}</dd></div><div><dt>更新时间</dt><dd>{formatTaskTime(batch?.completed_at || batch?.started_at || task.updatedAt)}</dd></div><div><dt>{task.isBatch ? '批次 ID' : '任务 ID'}</dt><dd>{task.isBatch ? task.title : task.taskIds[0]}</dd></div></dl>
    {failure && <div className="th-task-failure"><strong>失败原因</strong><span>{failure}</span></div>}
    {(batch?.completed || task.artifacts.length) > 0 && <div className="th-task-artifacts"><strong>产物</strong><span>{(batch?.completed || task.artifacts.length).toLocaleString()} 个 Trace</span></div>}
  </article>; })}</section>;
}

export function InfraHealthPage() {
  const { projects, project, setProject, projectError } = useInfraProject();
  const savedProject = localStorage.getItem('trace-hunter-infra-project') || '';
  const initialReport = savedProject ? peekCachedQuery<ObservabilityReport>(cacheKey('observability', savedProject)) : undefined;
  const [report, setReport] = useState<ObservabilityReport | undefined>(initialReport);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(!initialReport);
  const forceRefresh = useRef(false);
  const [refreshToken, setRefreshToken] = useState(0);
  useEffect(() => {
    if (!project) return;
    let active = true;
    const key = cacheKey('observability', project);
    const cached = peekCachedQuery<ObservabilityReport>(key);
    if (cached) setReport(cached); else setReport(undefined);
    setBusy(!cached); setError('');
    const force = forceRefresh.current;
    forceRefresh.current = false;
    loadCachedQuery(key, () => infraApi.observability(project), { ttlMs: 15_000, force })
      .then(value => { if (active) setReport(value); })
      .catch(reason => { if (active) setError(reason.message); })
      .finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [project, refreshToken]);
  const counts = report?.counts;
  const cards = [
    { label: '时间信息', known: counts?.timed_spans || 0, total: counts?.spans || 0 },
    { label: '状态信息', known: counts?.known_status_spans || 0, total: counts?.spans || 0 },
    { label: '上下文', known: counts?.contextual_model_spans || 0, total: counts?.model_spans || 0 },
    { label: 'Token', known: counts?.tokenized_model_spans || 0, total: counts?.model_spans || 0 },
  ];
  const projection = Object.entries(report?.projection || {});
  const coverage = Object.entries(report?.capture_coverage || {});
  const projectionLabels: Record<string,string> = { complete: '已完成', unindexed: '未索引', failed: '失败' };
  const coverageLabels: Record<string,string> = { tools: '工具调用', model_requests: '模型请求', messages: '消息', contexts: '上下文', timing: '时间信息' };
  const coverageStateLabels = { complete: '完整', partial: '部分', missing: '缺失', unknown: '未知' } as const;
  const coverageStates = ['complete', 'partial', 'missing', 'unknown'] as const;
  const rawTrigram = typeof report?.trigram_index === 'object' ? report.trigram_index.state || report.trigram_index.name || 'available' : report?.trigram_index;
  const trigram = rawTrigram === true || ['active','available','ready'].includes(String(rawTrigram)) ? '已启用' : rawTrigram === false || ['inactive','disabled','unavailable'].includes(String(rawTrigram)) ? '未启用' : String(rawTrigram ?? '—');
  return <div className="th-infra-page"><ProjectBar projects={projects} project={project} setProject={setProject}><button disabled={busy} onClick={() => { forceRefresh.current = true; setRefreshToken(value => value + 1); }}>{busy && report ? '刷新中' : '刷新真实数据'}</button></ProjectBar><ErrorNotice value={projectError || error}/>{busy && !report ? <InfraBusy/> : report && <><section className="th-storage-health"><Metric label="数据库" value={report.database}/><Metric label="搜索后端" value={report.search_backend}/><Metric label="Trigram 文本索引" value={trigram}/><Metric label="轨迹" value={String(counts?.traces ?? 0)}/><Metric label="事件" value={String(counts?.spans ?? 0)}/><Metric label="搜索文档" value={String(counts?.search_documents ?? 0)}/></section><section className="th-quality-grid">{cards.map(card => { const percent = card.total ? Math.round(card.known / card.total * 100) : 0; return <article key={card.label}><header><strong>{card.label}</strong><b>{percent}%</b></header><i><b style={{ width: `${percent}%` }}/></i><span>{card.known.toLocaleString()} / {card.total.toLocaleString()}</span></article>; })}</section><section className="th-health-facts"><article><header>对象统计</header><dl><div><dt>全部对象</dt><dd>{counts?.objects.toLocaleString()}</dd></div><div><dt>消息</dt><dd>{counts?.messages.toLocaleString()}</dd></div><div><dt>上下文</dt><dd>{counts?.contexts.toLocaleString()}</dd></div><div><dt>工具调用</dt><dd>{counts?.tool_calls.toLocaleString()}</dd></div><div><dt>关系</dt><dd>{counts?.edges.toLocaleString()}</dd></div></dl></article><article><header>索引状态</header><dl>{projection.map(([key,value]) => <div key={key}><dt>{projectionLabels[key] || key}</dt><dd>{String(value ?? '—')}</dd></div>)}</dl></article><article className="th-health-coverage"><header>采集覆盖率</header><div className="th-health-coverage-table" role="table" aria-label="采集覆盖率"><div className="th-health-coverage-head" role="row"><span role="columnheader">维度</span>{coverageStates.map(state => <span role="columnheader" key={state}>{coverageStateLabels[state]}</span>)}</div>{coverage.map(([key,value]) => <div className="th-health-coverage-row" role="row" key={key}><strong role="rowheader">{coverageLabels[key] || key}</strong>{coverageStates.map(state => <span role="cell" key={state}>{value[state] ?? 0}</span>)}</div>)}</div></article></section></>}</div>;
}
