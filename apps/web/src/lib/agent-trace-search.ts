import type { SearchItem, SearchResult } from '../api/infra-client';

export type ProjectSearchItem = SearchItem & { project_id?: string };
export type ProjectSearchResult = Omit<SearchResult, 'items'> & { items: ProjectSearchItem[] };

type Totals = Pick<SearchResult, 'candidate_count' | 'total_count' | 'matched_trace_count' | 'truncated'>;
type CursorState = { version: 1; pending: Record<string, string | null>; totals: Totals };

function cursorState(token: string | null, projects: string[]): CursorState {
  if (!token) return {
    version: 1,
    pending: Object.fromEntries(projects.map(project => [project, null])),
    totals: { candidate_count: 0, total_count: 0, matched_trace_count: 0, truncated: false },
  };
  const state = JSON.parse(token) as CursorState;
  if (state.version !== 1 || !state.pending || !state.totals ||
      Object.keys(state.pending).some(project => !projects.includes(project))) {
    throw new Error('Agent Trace 搜索游标无效，请重新搜索');
  }
  return state;
}

export async function searchAgentTraceProjects(
  projectIds: string[], cursor: string | null,
  search: (projectId: string, cursor: string | null, limit: number) => Promise<SearchResult>,
): Promise<ProjectSearchResult> {
  const state = cursorState(cursor, projectIds);
  const entries = Object.entries(state.pending);
  const limit = Math.max(1, Math.floor(24 / Math.max(1, projectIds.length)));
  const pages: Array<{ project_id: string; page: SearchResult }> = new Array(entries.length);
  let index = 0;
  await Promise.all(Array.from({ length: Math.min(6, entries.length) }, async () => {
    while (index < entries.length) {
      const slot = index++;
      const [projectId, nextCursor] = entries[slot];
      pages[slot] = { project_id: projectId, page: await search(projectId, nextCursor, limit) };
    }
  }));
  const pending: Record<string, string> = {};
  const totals = { ...state.totals };
  for (const { project_id, page } of pages) {
    if (page.next_cursor) pending[project_id] = page.next_cursor;
    if (!cursor) {
      totals.candidate_count += page.candidate_count;
      totals.total_count += page.total_count;
      totals.matched_trace_count += page.matched_trace_count;
      totals.truncated ||= page.truncated;
    }
  }
  const first = pages[0]?.page;
  return {
    items: pages.flatMap(({ project_id, page }) => page.items.map(item => ({ ...item, project_id }))),
    next_cursor: Object.keys(pending).length ? JSON.stringify({ version: 1, pending, totals }) : null,
    query_digest: 'agent-traces-group',
    projector_version: first?.projector_version || 'agent-traces-group',
    effective_pattern: first?.effective_pattern || null,
    ...totals,
  };
}
