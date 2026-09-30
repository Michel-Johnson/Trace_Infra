export type TerminalSessionRecord = { id: string; title: string; mtime: number; attached: boolean };
export type InfraSessionRecord = {
  session_id: string;
  claude_session_id?: string | null;
  native_terminal?: boolean;
  project_id: string;
  kind: 'auto_import' | 'agent';
  source_name: string | null;
  state: string | null;
  task_id: string | null;
  updated_at: string;
};

export type AgentSessionEntry = {
  key: string;
  kind: 'interactive' | 'auto_import';
  sessionId: string;
  label: string;
  updatedAt: number;
  state: string | null;
  projectId: string | null;
  taskId: string | null;
};

export function unifyAgentSessions(terminals: TerminalSessionRecord[], infra: InfraSessionRecord[]): AgentSessionEntry[] {
  const interactive = terminals.filter(item => /^[0-9a-f-]{36}$/i.test(item.id)).map(item => ({
    key: `interactive:${item.id}`, kind: 'interactive' as const, sessionId: item.id,
    label: item.title?.trim() || item.id.slice(0, 8),
    updatedAt: Number.isFinite(item.mtime) ? (item.mtime < 1e12 ? item.mtime * 1000 : item.mtime) : 0,
    state: item.attached ? 'running' : 'idle', projectId: null, taskId: null,
  }));
  const native = infra.filter(item => item.native_terminal === true && item.claude_session_id === item.session_id &&
    /^[0-9a-f-]{36}$/i.test(item.session_id)).map(item => ({
    key: `${item.kind}:${item.session_id}`, kind: 'auto_import' as const, sessionId: item.session_id,
    label: item.source_name ? `导入 · ${item.source_name}` : `Claude · ${item.project_id}`,
    updatedAt: Date.parse(item.updated_at) || 0,
    state: item.state, projectId: item.project_id, taskId: item.task_id,
  }));
  return [...interactive, ...native].sort((a, b) => b.updatedAt - a.updatedAt || a.key.localeCompare(b.key));
}
