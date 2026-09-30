import type { InfraTask } from '../api/infra-client';

export type TaskAgentTarget = {
  sessionId: string;
  kind: 'interactive' | 'native_import';
};

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function taskAgentTarget(task: InfraTask): TaskAgentTarget | null {
  if (!task.source || typeof task.source !== 'object') return null;
  const source = task.source as Record<string, unknown>;
  const sessionId = source.agent_session_id;
  const kind = source.agent_session_kind;
  if (typeof sessionId !== 'string' || !uuid.test(sessionId) ||
      (kind !== 'interactive' && kind !== 'native_import')) return null;
  return { sessionId, kind };
}

export function taskAgentHref(pathname: string, search: string, target: TaskAgentTarget) {
  const params = new URLSearchParams(search);
  params.set('agent_session', target.sessionId);
  params.set('agent_session_kind', target.kind);
  return { pathname, search: params.toString() };
}

type SessionRow = {
  session_id: string; task_id: string | null; project_id: string;
  native_terminal?: boolean; claude_session_id?: string | null;
};

let sessionCache: { until: number; request: Promise<SessionRow[]> } | null = null;

export async function loadTaskAgentSessions(): Promise<SessionRow[]> {
  if (sessionCache && Date.now() < sessionCache.until) return sessionCache.request;
  const request = fetch('/api/v1/agent/capabilities', { credentials: 'same-origin' })
    .then(response => response.ok
      ? fetch('/api/v1/agent/sessions', { credentials: 'same-origin' }) : null)
    .then(response => response?.ok ? response.json() as Promise<{ items?: SessionRow[] }> : { items: [] })
    .then(value => Array.isArray(value.items) ? value.items : [])
    .catch(() => [] as SessionRow[]);
  sessionCache = { until: Date.now() + 10_000, request };
  return request;
}

export function linkTasksToAgentSessions(tasks: InfraTask[], sessions: SessionRow[]): InfraTask[] {
  const byTask = new Map(sessions.filter(row => row.task_id && uuid.test(row.session_id))
    .map(row => [`${row.project_id}\0${row.task_id}`, row]));
  return tasks.map(task => {
    if (taskAgentTarget(task)) return task;
    const source = task.source as Record<string, unknown> | null;
    if (!source || !['agent', 'auto_import'].includes(String(source.type))) return task;
    const row = byTask.get(`${task.project_id}\0${task.task_id}`);
    if (!row) return task;
    const native = row.native_terminal === true && row.claude_session_id === row.session_id;
    if (!native) return task;
    return { ...task, source: { ...source, agent_session_id: row.session_id,
      agent_session_kind: 'native_import' } };
  });
}
