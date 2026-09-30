import type {Bundle, Row} from '../api/types';

export const CALL_PAGE_SIZE = 48;
export const GROUP_PAGE_SIZE = 8;
export const statusMatches = (row:Row, status:string) =>
  status === 'all' || (status === 'missing' ? row.tool_ms == null : row.status === status);

/** Bound the renderer input after filtering/sorting; original objects and indices stay intact. */
export function pageEntries<T>(entries: T[], requestedPage = 1, pageSize = CALL_PAGE_SIZE) {
  const size = Number.isFinite(pageSize) && pageSize > 0 ? Math.floor(pageSize) || 1 : CALL_PAGE_SIZE;
  const pages = Math.max(1, Math.ceil(entries.length / size));
  const page = Math.min(pages, Math.max(1, Number.isFinite(requestedPage) ? Math.floor(requestedPage) : 1));
  const offset = (page - 1) * size;
  return {entries: entries.slice(offset, offset + size), page, pageSize: size, total: entries.length,
    first: entries.length ? offset + 1 : 0, last: Math.min(offset + size, entries.length)};
}

/** User turns come only from the API's source-message mapping, never source-file count. */
export function turnMatches(bundle: Bundle, row: Row, selection: string) {
  if (selection === 'all') return true;
  const turn = bundle.view.span_turns?.[row.span_id];
  if (selection === 'unknown') return !turn || turn.coverage === 'missing';
  return !!turn && turn.coverage !== 'missing' && String(turn.ordinal) === selection;
}

export function recordedTurns(bundles: Bundle[]) {
  return Array.from(new Set(bundles.flatMap(b => b.view.rows.flatMap(row => {
    const turn = b.view.span_turns?.[row.span_id];
    return turn && turn.coverage !== 'missing' ? [turn.ordinal] : [];
  })))).sort((a, b) => a - b);
}
