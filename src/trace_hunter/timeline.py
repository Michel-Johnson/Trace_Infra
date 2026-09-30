"""Read-only trace presentation. No token totals, scoring, or analysis jobs."""
from .run_summary import summarize


def duration(span):
    start, end = span['start_ms'], span['end_ms']
    return end - start if start is not None and end is not None else span['duration_ms']


def select_phases(trace, phase_ids=None):
    selected = phase_ids if phase_ids is not None else [
        p['id'] for p in trace['phases'] if p['purpose'] == 'task']
    allowed = {p['id'] for p in trace['phases']}
    if not selected or any(p not in allowed for p in selected):
        raise ValueError('请选择有效阶段')
    return [p for p in trace['phases'] if p['id'] in selected]


def tool_rows(trace, phases):
    phase_map = {p['id']: p for p in phases}
    spans = [s for s in trace['spans'] if s['phase_id'] in phase_map]
    tools = [s for s in spans if s['kind'] == 'tool']
    models = {s['id']: s for s in spans if s['kind'] == 'model'}
    invokes = {e['to']: e['from'] for e in trace['links'] if e['type'] == 'invokes'}
    # Complete sequence evidence includes untimed calls. Otherwise preserve array
    # order when any timestamp is missing; do not push unknown calls to the end.
    if tools and all('sequence' in s for s in tools):
        ordered = sorted(tools, key=lambda s: s['sequence'])
    elif all(s['start_ms'] is not None for s in tools):
        ordered = sorted(tools, key=lambda s: s['start_ms'])
    else:
        ordered = tools

    previous = {}
    rows = []
    for span in ordered:
        start, end = span['start_ms'], span['end_ms']
        elapsed = duration(span)
        linked = models.get(invokes.get(span['id']))
        response = duration(linked) if linked else None
        basis = 'measured' if response is not None else 'missing'
        key = (span['phase_id'], span['agent_id'])
        anchor = previous.get(key, phase_map[span['phase_id']]['start_ms'])
        if response is None and start is not None and anchor is not None and start >= anchor:
            waits = [w['end_ms'] for w in spans if w['kind'] == 'wait'
                     and (w['phase_id'], w['agent_id']) == key
                     and w['end_ms'] is not None and w['end_ms'] <= start]
            response = start - max([anchor] + waits)
            basis = 'interval_estimate'
        if end is not None:
            previous[key] = max(previous.get(key, end), end)
        rows.append({
            'span_id': span['id'], 'name': span['name'], 'operation': span['operation'],
            'status': span['status'], 'phase_id': span['phase_id'], 'tool_ms': elapsed,
            'response_ms': response, 'response_basis': basis,
            'cycle_ms': elapsed + response if elapsed is not None and response is not None else None,
            'shared_request': invokes.get(span['id']), 'skill': span.get('skill'),
            'order_basis': span.get('order_basis', 'timestamp' if span['start_ms'] is not None else 'input_order'),
            'sequence': span.get('sequence')})
    return rows


def view(trace, phase_ids=None):
    phases = select_phases(trace, phase_ids)
    rows = tool_rows(trace, phases)
    scope = {p['id'] for p in phases}
    ids = {s['id'] for s in trace['spans'] if s['phase_id'] in scope}
    return {'scope': [p['id'] for p in phases], 'summary': summarize(trace, phases), 'rows': rows,
            'span_turns': {sid: turn for sid, turn in trace.get('_span_turns', {}).items() if sid in ids},
            'tool_count': len(rows),
            'tool_time_known': sum(r['tool_ms'] is not None for r in rows)}
