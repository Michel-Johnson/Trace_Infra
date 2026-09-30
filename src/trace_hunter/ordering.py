"""Order evidence and skill labels; no invented execution timestamps."""
import copy
import hashlib
import json
from pathlib import Path


def skill_label(span):
    if span.get('skill'):
        return span['skill']
    value = span.get('input')
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = {}
    value = value if isinstance(value, dict) else {}
    if span['name'].lower() == 'skill':
        return {'name': str(value.get('skill') or value.get('name') or '未记录技能名'), 'action': 'invoke'}
    if span['name'].lower() == 'read':
        path = value.get('file_path') or value.get('path')
        if isinstance(path, str) and Path(path).name == 'SKILL.md':
            return {'name': Path(path).parent.name, 'action': 'load'}
        if value.get('tail') == 'SKILL.md':
            return {'name': '未记录技能名', 'action': 'load'}
    return None


def merge_order(spans, native_ids):
    """Known timestamps anchor the order. Untimed native calls retain message order.

    The position within an unobserved gap is not an exact timestamp. Place an
    untimed call next to the following recorded anchor, or after its preceding
    anchor when there is no following one. Never sort it by its UUID.
    """
    tools = [s for s in spans if s['kind'] == 'tool']
    known = sorted((s for s in tools if s['start_ms'] is not None), key=lambda s: s['start_ms'])
    ordered = [s['id'] for s in known]
    all_ids = {s['id'] for s in tools}
    native_ids = [sid for sid in native_ids if sid in all_ids]
    for index, sid in enumerate(native_ids):
        if sid in ordered:
            continue
        following = next((x for x in native_ids[index + 1:] if x in ordered), None)
        preceding = next((x for x in reversed(native_ids[:index]) if x in ordered), None)
        if following is not None:
            position = ordered.index(following)
            if preceding is not None and ordered.index(preceding) >= position:
                # Conflicting source clocks cannot establish an exact placement.
                position = ordered.index(preceding) + 1
        elif preceding is not None:
            position = ordered.index(preceding) + 1
        else:
            position = len(ordered)
        ordered.insert(position, sid)
    ordered.extend(s['id'] for s in tools if s['id'] not in ordered)
    ranks = {sid: i for i, sid in enumerate(ordered)}
    for span in tools:
        span['sequence'] = ranks[span['id']]
        span['order_basis'] = ('timestamp' if span['start_ms'] is not None else
                               'source_sequence' if span['id'] in native_ids else 'unknown')
    return spans


def decorate(trace, native_ids=None):
    if native_ids is not None:
        merge_order(trace['spans'], native_ids)
    for span in trace['spans']:
        skill = skill_label(span)
        if skill:
            span['skill'] = skill
    return trace


def source_projection(trace):
    """Read old imports using their hash-verified original export; leave payload intact."""
    projected = copy.deepcopy(trace)
    tools = [s for s in projected['spans'] if s['kind'] == 'tool']
    needs_order = tools and any('sequence' not in s for s in tools)
    from .protocol import ROOT
    from .run_summary import conversation_projection, span_turn_projection
    allowed = {
        'doubao-2.1-pro.trajectory.json': 'doubao-export',
        'orange-5.0.trajectory.json': 'doubao-export',
        'doubao-2.1-pro.sqlite.trajectory.json': 'doubao-export',
        'orange-5.0.sqlite.trajectory.json': 'doubao-export',
        'claude-orange-5.execution-trace.json': 'claude-export',
    }
    for source in trace['sources']:
        if source['name'] not in allowed:
            continue
        path = ROOT / 'apps/trace-lab' / source['name']
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if hashlib.sha256(raw).hexdigest() != source['sha256']:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        projected['_conversation_projection'] = conversation_projection(projected, data, allowed[source['name']])
        projected['_span_turns'] = span_turn_projection(projected, data, allowed[source['name']], source['id'])
        if needs_order:
            from .adapters import normalize
            normalized = normalize(path, allowed[source['name']])
            annotations = {s['id']: s for s in normalized['spans']}
            for span in projected['spans']:
                if span['id'] in annotations:
                    for key in ('sequence', 'order_basis', 'skill'):
                        if key not in span and key in annotations[span['id']]:
                            span[key] = annotations[span['id']][key]
        break
    return decorate(projected)
