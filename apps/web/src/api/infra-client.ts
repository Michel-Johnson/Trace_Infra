import type { components } from './generated';

export type InfraProject = { project_id: string; name: string; created_at: string };

export type SearchMode = 'literal' | 'regex';
export type SearchField = 'name' | 'input' | 'output' | 'content' | 'request' | 'arguments';
export type SearchObjectKind = 'span' | 'message' | 'context' | 'tool_call';
export type MatchRange = { start: number; end: number };
export type SourceReference = { pointer: string; source_id?: string | null };

export type SearchCapabilities = {
  version: string;
  modes: SearchMode[];
  scopes: ('analysis' | 'model_context')[];
  object_kinds: SearchObjectKind[];
  fields: SearchField[];
  structured_filters?: string[];
  max_page_size: number;
  max_scan_candidates: number;
  max_snippet_chars: number;
  max_match_ranges: number;
  projector_version: string;
  backend: 'postgresql_pg_trgm' | 'sqlite_scan_fallback';
  regex_dialect: 'postgresql_are';
  regex_syntaxes: ('postgresql_are' | 'portable')[];
  regex_max_chars: number;
  regex_examples: { intent: string; pattern: string; regex_syntax?: string }[];
  source_content: 'inline_only';
  triggers_analysis: false;
};

export type SearchFilters = {
  run_id?: string[];
  object_kind?: SearchObjectKind[];
  field?: SearchField[];
  span_id?: string[];
  skill_name?: string[];
  skill_action?: string[];
  name?: string[];
  operation?: string[];
  status?: string[];
};

export type SearchItem = {
  run_id: string;
  revision: number;
  object_kind: SearchObjectKind;
  object_id: string;
  span_id: string | null;
  field: SearchField;
  text_state: 'exact' | 'truncated';
  score: number;
  snippet: string;
  snippet_start: number;
  text_length: number;
  snippet_truncated: boolean;
  match_ranges: MatchRange[];
  source_refs: SourceReference[];
};

export type SearchResult = {
  items: SearchItem[];
  next_cursor: string | null;
  query_digest: string;
  projector_version: string;
  effective_pattern: string | null;
  candidate_count: number;
  total_count: number;
  matched_trace_count: number;
  truncated: boolean;
};

export type SpanItem = {
  run_id: string | null;
  revision: number | null;
  source_ordinal: number | null;
  span_id: string | null;
  kind: string | null;
  name: string | null;
  operation: string | null;
  status: string | null;
  parent_id: string | null;
  start_ms: number | null;
  end_ms: number | null;
  duration_ms: number | null;
  duration_basis: string | null;
  duration_scope: string | null;
  skill_name: string | null;
  skill_action: string | null;
  context_id: string | null;
  visibility_status: string | null;
  source_refs: SourceReference[] | null;
};

export type SpanFilters = {
  run_id?: string[];
  kind?: string[];
  name?: string[];
  operation?: string[];
  status?: string[];
  skill_name?: string[];
};

export type SpanQueryResult = {
  items: SpanItem[];
  next_cursor: string | null;
  query_digest: string;
  projector_version: string;
  consistency: 'live_keyset';
};

export type RawTraceSpan = {
  id?: string;
  span_id?: string;
  kind?: string | null;
  name?: string | null;
  operation?: string | null;
  status?: string | null;
  parent_id?: string | null;
  request_id?: string | null;
  context_id?: string | null;
  start_ms?: number | null;
  end_ms?: number | null;
  duration_ms?: number | null;
  input?: unknown;
  output?: unknown;
  result?: unknown;
  schema?: unknown;
  usage?: Record<string, unknown> | null;
  source?: SourceReference | null;
  source_refs?: SourceReference[] | null;
  skill?: { name?: string | null; action?: string | null } | null;
  timing?: Record<string, unknown> | null;
  [key: string]: unknown;
};

export type RawTraceDocument = {
  schema_version?: string;
  format_version?: string;
  run?: Record<string, unknown>;
  spans?: RawTraceSpan[];
  messages?: Array<Record<string, unknown>>;
  tool_calls?: Array<Record<string, unknown>>;
  contexts?: Array<Record<string, unknown>>;
  events?: Array<Record<string, unknown>>;
  turns?: Array<Record<string, unknown>>;
  [key: string]: unknown;
};

export type TraceItem = {
  run_id: string | null;
  revision: number | null;
  content_digest: string | null;
  format_version: string | null;
  created_at: string | null;
  query_id: string | null;
  env_id: string | null;
  harness: string | null;
  model: string | null;
  status: string | null;
  index_state: string | null;
  record_count: number | null;
  model_count: number | null;
  tool_count: number | null;
  indexed_at: string | null;
};

export type TraceQueryResult = {
  items: TraceItem[];
  next_cursor: string | null;
  query_digest: string;
  projector_version: string;
  consistency: 'live_keyset';
};

export type ImportedTrace = {
  project_id: string;
  run_id: string;
  revision: number;
  content_digest: string;
  format_version: string;
  index_state: string;
  record_count: number | null;
};

export type AdapterImportCapabilities = {
  canonical_schema: string;
  adapters: Array<{ format: string; binding_required: Array<'run_id' | 'query_id' | 'env_id'> }>;
  stages: Array<{ id: string; label: string }>;
  max_source_bytes: number;
  progress_persistence: string;
};

export type ObservabilityReport = {
  database: string;
  search_backend: string;
  trigram_index: string | boolean | { state?: string; name?: string };
  counts: {
    traces: number;
    objects: number;
    spans: number;
    messages: number;
    contexts: number;
    tool_calls: number;
    search_documents: number;
    edges: number;
    timed_spans: number;
    known_status_spans: number;
    model_spans: number;
    contextual_model_spans: number;
    tokenized_model_spans: number;
  };
  projection: Record<string, number | string | null>;
  capture_coverage: Record<string, Record<'complete' | 'partial' | 'missing' | 'unknown', number>>;
};

export type TaskKind = 'adapter_import' | 'evaluation' | 'analysis' | 'custom';
export type TaskState = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
export type TaskStep = {
  id?: string;
  label?: string;
  state?: string;
  started_at?: string | null;
  completed_at?: string | null;
  details?: Record<string, unknown> | null;
};
export type TaskEvent = { at?: string; type?: string; message?: string; [key: string]: unknown };
export type TaskArtifact = { kind?: string; name?: string; url?: string; path?: string; [key: string]: unknown };
export type InfraTask = {
  task_id: string;
  project_id: string;
  kind: TaskKind;
  title: string;
  state: TaskState;
  current_stage: string | null;
  progress: number;
  processed: number;
  total: number | null;
  created_at: string;
  started_at: string | null;
  updated_at: string;
  completed_at: string | null;
  revision: number;
  steps: TaskStep[];
  events: TaskEvent[];
  artifacts: TaskArtifact[];
  result: unknown;
  error: string | { code?: string; message?: string; details?: unknown } | null;
  source: unknown;
};

export type TaskListResult = { items: InfraTask[]; total: number; truncated: boolean; next_cursor: string | null; retention_limit: number };
export type TaskListQuery = { limit?: number; after?: string | null; state?: TaskState[]; kind?: TaskKind[] };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const raw = await response.text();
  const value = raw ? JSON.parse(raw) : null;
  if (!response.ok) throw Object.assign(new Error(value?.error || value?.detail || `HTTP ${response.status}`),
    { status: response.status, code: value?.code });
  return value as T;
}

async function requestBlob(path: string, init?: RequestInit) {
  const response = await fetch(path, init);
  if (!response.ok) {
    const raw = await response.text();
    let value: { error?: string; detail?: string } = {};
    try { value = raw ? JSON.parse(raw) : {}; } catch { value = {}; }
    throw new Error(value.error || value.detail || `HTTP ${response.status}`);
  }
  return response.blob();
}

const jsonInit = (body: unknown, signal?: AbortSignal): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
  signal,
});

export const infraApi = {
  projects: (signal?: AbortSignal, after?: string | null) => request<{ items: InfraProject[]; next_after: string | null }>(`/api/v1/projects?limit=100${after ? `&after=${encodeURIComponent(after)}` : ''}`, { signal }),
  searchCapabilities: (signal?: AbortSignal) => request<SearchCapabilities>('/api/v1/trace-search-capabilities', { signal }),
  adapterImportCapabilities: (signal?: AbortSignal) => request<AdapterImportCapabilities>('/api/v1/adapter-import-capabilities', { signal }),
  search: (project: string, body: { query: string; mode: SearchMode; regex_syntax?: 'postgresql_are' | 'portable'; scope: 'analysis'; filters?: SearchFilters; revisions?: 'latest' | 'all'; limit?: number; cursor?: string | null }, signal?: AbortSignal) =>
    request<SearchResult>(`/api/v1/projects/${encodeURIComponent(project)}/search`, jsonInit(body, signal)),
  spans: (project: string, body: { filters?: SpanFilters; fields: string[]; revisions?: 'latest' | 'all'; order?: 'source' | 'duration_desc'; limit?: number; cursor?: string | null }, signal?: AbortSignal) =>
    request<SpanQueryResult>(`/api/v1/projects/${encodeURIComponent(project)}/spans/query`, jsonInit(body, signal)),
  traces: (project: string, body: { fields: string[]; revisions?: 'latest' | 'all'; limit?: number; cursor?: string | null }, signal?: AbortSignal) =>
    request<TraceQueryResult>(`/api/v1/projects/${encodeURIComponent(project)}/traces/query`, jsonInit(body, signal)),
  agentTraceLabels: (items: Array<{ project_id: string; run_id: string }>, signal?: AbortSignal) =>
    request<{ items: Array<{ project_id: string; run_id: string; title: string }> }>('/api/v1/agent/trace-labels', jsonInit({ items }, signal)),
  traceContent: (project: string, runId: string, revision: number, signal?: AbortSignal) =>
    request<RawTraceDocument>(`/api/v1/projects/${encodeURIComponent(project)}/traces/${encodeURIComponent(runId)}/revisions/${revision}/content`, { signal }),
  agentTraceSource: (project: string, runId: string, revision: number, sourceId: string, signal?: AbortSignal) =>
    request<unknown>(`/api/v1/projects/${encodeURIComponent(project)}/traces/${encodeURIComponent(runId)}/revisions/${revision}/sources/${encodeURIComponent(sourceId)}/content`, { signal }),
  createUpload: (project: string, body: UploadCreate, signal?: AbortSignal) => request<UploadStatus>(`/api/v1/projects/${encodeURIComponent(project)}/imports/uploads`, jsonInit(body, signal)),
  uploadStatus: (project: string, uploadId: string, signal?: AbortSignal) => request<UploadStatus>(`/api/v1/projects/${encodeURIComponent(project)}/imports/uploads/${encodeURIComponent(uploadId)}`, { signal }),
  uploadPart: (project: string, uploadId: string, position: number, chunk: Blob, sha256: string, signal?: AbortSignal) => request<UploadStatus>(`/api/v1/projects/${encodeURIComponent(project)}/imports/uploads/${encodeURIComponent(uploadId)}/parts/${position}`, { method: 'PUT', headers: { 'Content-Type': 'application/octet-stream', 'X-Chunk-SHA256': sha256 }, body: chunk, signal }),
  completeUpload: (project: string, uploadId: string, signal?: AbortSignal) => request<UploadStatus>(`/api/v1/projects/${encodeURIComponent(project)}/imports/uploads/${encodeURIComponent(uploadId)}/complete`, { method: 'POST', signal }),
  agentCapabilities: (signal?: AbortSignal) => request<{ enabled: boolean }>('/api/v1/agent/capabilities', { signal }),
  taskStatus: (project: string, taskId: string, signal?: AbortSignal) => request<InfraTask>(`/api/v1/projects/${encodeURIComponent(project)}/tasks/${encodeURIComponent(taskId)}`, { signal }),
  importAdapters: (project: string, signal?: AbortSignal) => request<{ items: Array<{ name: string; content_ref: { digest: string; size_bytes: number }; task_id: string }> }>(`/api/v1/projects/${encodeURIComponent(project)}/import-adapters`, { signal }),
  importBatch: (project: string, batchId: string, query: { limit?: number; after?: string | null } = {}, signal?: AbortSignal) => {
    const params = new URLSearchParams({ limit: String(query.limit || 100) });
    if (query.after) params.set('after', query.after);
    return request<ImportBatch>(`/api/v1/projects/${encodeURIComponent(project)}/import-batches/${encodeURIComponent(batchId)}?${params}`, { signal });
  },
  observability: (project: string, signal?: AbortSignal) => request<ObservabilityReport>(`/api/v1/projects/${encodeURIComponent(project)}/observability`, { signal }),
  tasks: (project: string, query: TaskListQuery = {}, signal?: AbortSignal) => {
    const params = new URLSearchParams({ limit: String(query.limit || 200) });
    if (query.after) params.set('after', query.after);
    query.state?.forEach(value => params.append('state', value));
    query.kind?.forEach(value => params.append('kind', value));
    return request<TaskListResult>(`/api/v1/projects/${encodeURIComponent(project)}/tasks?${params}`, { signal });
  },
  retryTask: (project: string, taskId: string, signal?: AbortSignal) => request<InfraTask>(`/api/v1/projects/${encodeURIComponent(project)}/tasks/${encodeURIComponent(taskId)}/retry`, { method: 'POST', signal }),
  cancelTask: (project: string, taskId: string, signal?: AbortSignal) => request<InfraTask>(`/api/v1/projects/${encodeURIComponent(project)}/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST', signal }),
  taskEventsUrl: (project: string, taskId: string) => `/api/v1/projects/${encodeURIComponent(project)}/tasks/${encodeURIComponent(taskId)}/events`,
  spanWindow: (project: string, body: SpanWindowRequest, signal?: AbortSignal) => request<SpanWindowResult>(`/api/v1/projects/${encodeURIComponent(project)}/spans/window`, jsonInit(body, signal)),
  objects: (project: string, body: ObjectQueryRequest, signal?: AbortSignal) => request<GenericResult>(`/api/v1/projects/${encodeURIComponent(project)}/objects/query`, jsonInit(body, signal)),
  metrics: (project: string, body: MetricsRequest, signal?: AbortSignal) => request<GenericResult>(`/api/v1/projects/${encodeURIComponent(project)}/metrics/query`, jsonInit(body, signal)),
  evidenceExport: (project: string, body: EvidenceExportRequest, signal?: AbortSignal) => request<InfraTask>(`/api/v1/projects/${encodeURIComponent(project)}/evidence-exports`, jsonInit(body, signal)),
  evidenceExportContent: (project: string, taskId: string, signal?: AbortSignal) => requestBlob(`/api/v1/projects/${encodeURIComponent(project)}/evidence-exports/${encodeURIComponent(taskId)}/content`, { signal }),
  apiContract: (signal?: AbortSignal) => request<{ paths?: Record<string, unknown> }>('/api/openapi.json', { signal }),
};

export const traceFields = ['run_id','revision','content_digest','format_version','created_at','query_id','env_id','harness','model','status','index_state','record_count','model_count','tool_count','indexed_at'];
export const spanFields = ['run_id','revision','source_ordinal','span_id','kind','name','operation','status','parent_id','start_ms','end_ms','duration_ms','duration_basis','duration_scope','skill_name','skill_action','context_id','visibility_status','source_refs'];
export type UploadCreate = components['schemas']['UploadCreate'];
export type UploadStatus = components['schemas']['UploadStatus'];
export type ImportBatch = components['schemas']['ImportBatch'];
export type SpanWindowRequest = components['schemas']['SpanWindowRequest'];
export type SpanWindowResult = components['schemas']['SpanWindowResult'];
export type MetricsRequest = components['schemas']['MetricsRequest'];
export type EvidenceExportRequest = components['schemas']['EvidenceExportRequest'];
export type GenericResult = components['schemas']['GenericResult'];

export type ObjectQueryRequest = components['schemas']['ObjectQueryRequest'];
