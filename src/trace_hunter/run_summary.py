"""Read-only run header facts. No scoring, token analysis or Case-plan inference."""


def conversation_projection(trace, data, source_format):
    """Extract actual user inputs from a verified legacy export.

    This metadata stays in the read projection; it is never written into an
    imported payload. Source-specific role semantics belong here, not in the UI.
    """
    turns = []
    coverage = {}
    if source_format == 'claude-export':
        seen = set()
        for index, event in enumerate(data.get('timeline', [])):
            if event.get('kind') != 'user_message' or event.get('is_meta') is not False:
                continue
            # The export's row is the original JSONL position. Do not count a
            # repeated export snapshot as another user turn.
            key = event.get('row', index)
            if key in seen:
                continue
            seen.add(key)
            turns.append({'phase_id': str(event['phase']), 'source_index': index})
        source_phases = {str(p['id']) for p in data.get('phases', [])}
        for phase in trace['phases']:
            if phase['id'] in source_phases:
                # This legacy export includes finished task phases, followed by
                # an export phase still in progress when the file was written.
                coverage[phase['id']] = 'complete' if phase['purpose'] == 'task' else 'partial'
        basis = '源会话中的用户消息；排除技能注入、工具返回和重复记录。导出阶段采集未结束。'
    elif source_format == 'doubao-export' and isinstance(data.get('native_messages'), list):
        for index, message in enumerate(data['native_messages']):
            if message.get('role') != 'user' or message.get('is_meta') or message.get('isCompactSummary'):
                continue
            content = message.get('content')
            if isinstance(content, list) and content and all(isinstance(c, dict) and c.get('type') == 'tool_result' for c in content):
                continue
            turns.append({'phase_id': 'task', 'source_index': index})
        # These native snapshots do not assert complete end-to-end coverage.
        coverage['task'] = 'partial'
        basis = '源文件的原生用户消息；会话采集未声明完整，显示已记录轮数的下限。'
    else:
        return None
    return {'turns': turns, 'coverage': coverage, 'basis': basis}


def summarize(trace, phases):
    known_bounds = all(p['start_ms'] is not None and p['end_ms'] is not None for p in phases)
    wall_ms = max(p['end_ms'] for p in phases) - min(p['start_ms'] for p in phases) if known_bounds else None
    conversation = trace.get('_conversation_projection')
    turn_count, turn_coverage = None, 'missing'
    basis = '轨迹未记录可核对的用户对话轮次；不以 Case 预设轮数或工具调用数代替。'
    if conversation is not None:
        scope = {p['id'] for p in phases}
        states = [conversation['coverage'].get(pid, 'missing') for pid in scope]
        count = sum(t['phase_id'] in scope for t in conversation['turns'])
        if all(state == 'complete' for state in states):
            turn_count, turn_coverage = count, 'complete'
        elif count:
            turn_count, turn_coverage = count, 'partial'
        basis = conversation['basis']
    return {'wall_ms': wall_ms, 'user_turn_count': turn_count,
            'user_turn_coverage': turn_coverage, 'user_turn_basis': basis}


def span_turn_projection(trace, data, source_format, source_id):
    """Associate spans with observed user inputs using verified source positions.

    Ordinals are within this capture, not invented global session indices. Tool
    results, skill injection and compaction summaries never start a user turn.
    """
    conversation = conversation_projection(trace, data, source_format)
    if conversation is None:
        return {}
    starts = {t['source_index']: i + 1 for i, t in enumerate(conversation['turns'])}
    events = data.get('timeline', []) if source_format == 'claude-export' else data.get('native_messages', [])
    at_index, at_row, current = {}, {}, None
    for index, event in enumerate(events):
        if index in starts:
            current = starts[index]
        if current is not None:
            at_index[index] = current
            if 'row' in event:
                at_row[event['row']] = current
    native = {c.get('tool_call_id', c.get('id')): c for c in data.get('tool_calls', [])}
    result = {}
    for span in trace['spans']:
        if span['source']['source_id'] != source_id:
            continue
        parts = span['source']['pointer'].strip('/').split('/')
        ordinal = None
        if len(parts) >= 2 and parts[1].isdigit():
            key, index = parts[0], int(parts[1])
            if key in ('timeline', 'native_messages'):
                ordinal = at_index.get(index)
            elif key in ('tool_calls', 'native_tool_calls', 'runtime_tool_calls'):
                records = data.get(key, [])
                if index < len(records):
                    record = records[index]
                    if source_format == 'claude-export':
                        ordinal = at_row.get(record.get('row'))
                    else:
                        if key == 'runtime_tool_calls':
                            record = native.get(record.get('native_tool_call_id'), {})
                        ordinal = at_index.get(record.get('native_message_index'))
        if ordinal is not None:
            result[span['id']] = {'turn_id': f'{source_id}:user-{ordinal}', 'ordinal': ordinal,
                                  'coverage': conversation['coverage'].get(span['phase_id'], 'partial')}
    return result
