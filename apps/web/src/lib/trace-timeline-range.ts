export type TraceRange = { start: number; end: number };
export type TraceRangeEdge = 'start' | 'end';
export type TraceTimelineSpan = { start_ms: number | null; end_ms: number | null };

function ordered(range: TraceRange): TraceRange {
  return range.start <= range.end ? range : { start: range.end, end: range.start };
}

export function clampTraceRange(range: TraceRange, bounds: TraceRange): TraceRange {
  const limit = ordered(bounds);
  const value = ordered(range);
  return {
    start: Math.max(limit.start, Math.min(limit.end, value.start)),
    end: Math.max(limit.start, Math.min(limit.end, value.end)),
  };
}

export function moveTraceRange(range: TraceRange, delta: number, bounds: TraceRange): TraceRange {
  const limit = ordered(bounds);
  const value = clampTraceRange(range, limit);
  const width = Math.min(limit.end - limit.start, value.end - value.start);
  const start = Math.max(limit.start, Math.min(limit.end - width, value.start + delta));
  return { start, end: start + width };
}

export function resizeTraceRange(range: TraceRange, edge: TraceRangeEdge, value: number, bounds: TraceRange, minimum = 0): TraceRange {
  const limit = ordered(bounds);
  const current = clampTraceRange(range, limit);
  const minWidth = Math.max(0, Math.min(limit.end - limit.start, minimum));
  if (edge === 'start') {
    return { start: Math.max(limit.start, Math.min(current.end - minWidth, value)), end: current.end };
  }
  return { start: current.start, end: Math.min(limit.end, Math.max(current.start + minWidth, value)) };
}

export function intersectTraceRange(range: TraceRange, domain: TraceRange): TraceRange | null {
  const value = ordered(range);
  const visible = ordered(domain);
  const start = Math.max(value.start, visible.start);
  const end = Math.min(value.end, visible.end);
  return end > start ? { start, end } : null;
}

export function traceTimelineUsesDuration(spans: TraceTimelineSpan[], actualDuration: boolean) {
  return actualDuration && spans.length > 0 && spans.every(span => span.start_ms != null && span.end_ms != null);
}

export function traceSpanIntersectsRange(span: TraceTimelineSpan, index: number, range: TraceRange, useDuration: boolean) {
  const selected = ordered(range);
  const start = useDuration && span.start_ms != null ? span.start_ms : index;
  const end = useDuration && span.end_ms != null ? span.end_ms : index + 1;
  const item = ordered({ start, end });
  if (item.start === item.end) return item.start >= selected.start && item.start <= selected.end;
  return item.end > selected.start && item.start < selected.end;
}
