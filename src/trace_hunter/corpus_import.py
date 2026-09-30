"""Offline conversion of the Doubao Work builtin-traces command export.

One conversation becomes one partial run; message fragments remain phases.
This adapter never executes commands, infers model usage, or performs analysis.
"""
import collections
import hashlib
import json
import math
import re
from datetime import datetime

from .adapters import span
from .catalog import validate_catalog
from .identity import unknown_environment
from .ordering import merge_order
from .protocol import validate

VERSION = '1.3.0'
ORDER_POLICY = 'timestamps-with-source-anchors/1'
STATUS_POLICY = 'explicit-business-or-process-failure/1'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key: ' + key)
            result[key] = value
        return result

    def nonfinite(value):
        raise ValueError('Nonfinite JSON number: ' + value)

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)


def normalized_result(raw):
    """Decode a complete JSON result once; retain arbitrary text without guessing."""
    try:
        value = decode(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        value = raw
    decoded = isinstance(value, (dict, list))
    output = value if decoded else raw
    if isinstance(output, dict): candidates = [output]
    elif isinstance(output, list):
        candidates = [x['text'] for x in output if isinstance(x, dict)
                      and x.get('type') in ('text', 'input_text', 'output_text') and isinstance(x.get('text'), str)]
    else: candidates = [output]
    business_ok = []
    for candidate in candidates:
        if isinstance(candidate, str):
            # Only exact process markers around a complete JSON response are removed
            # for inspection. The output itself keeps those markers byte-for-byte.
            text = re.sub(r'^\s*Exit code -?\d+\s*\n', '', candidate)
            text = re.sub(r'\nExit code -?\d+\s*$', '', text)
            try: candidate = decode(text)
            except (ValueError, TypeError): continue
        if (isinstance(candidate, dict) and isinstance(candidate.get('ok'), bool)
                and (isinstance(candidate.get('identity'), str) or isinstance(candidate.get('error'), dict)
                     or 'data' in candidate)):
            business_ok.append(candidate['ok'])
    return output, {'result_encoding': 'json' if decoded else 'text',
                    'business_ok': business_ok}


def normalized_execution(group):
    results, facts = [], []
    for index, item in group:
        output, fact = normalized_result(item['result'])
        results.append(output)
        facts.append({'source_item_index': index, 'source_item_id': item.get('id'),
                      'seq': item.get('seq'), 'result_seq': item.get('resultSeq'),
                      'sub_index': item.get('subIndex'),
                      'source_description': item.get('description'),
                      'source_result_summary': item.get('resultSummary'),
                      'source_is_help': item.get('isHelp'),
                      'shared_duration': item.get('sharedDuration'),
                      'source_timing': {k: item.get(k) for k in ('startedAt', 'finishedAt', 'durationMs')},
                      'source_fields_present': sorted(item),
                      'source_operation': item.get('operation'), 'source_status': item.get('status'),
                      'process_exit_code': item.get('exitCode'), **fact})
    source_status = ('error' if any(f['source_status'] == 'failed' for f in facts)
                     else 'ok' if all(f['source_status'] == 'success' for f in facts) else 'unknown')
    business_failure = any(False in f['business_ok'] for f in facts)
    process_failure = any(isinstance(f['process_exit_code'], int) and not isinstance(f['process_exit_code'], bool)
                          and f['process_exit_code'] != 0 for f in facts)
    status = 'error' if business_failure or process_failure else source_status
    return results, {'items': facts, 'source_aggregate_status': source_status,
                     'normalized_status': status, 'status_policy': STATUS_POLICY,
                     'status_corrected': status != source_status,
                     'explicit_business_failure': business_failure, 'process_failure': process_failure}


def epoch(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', value):
        raise ValueError('Expected timestamp with timezone')
    # Python 3.9 accepts only 3 or 6 fractional digits. Exported timestamps also
    # contain 1, 2, 4 and 5 digits; padding changes no recorded precision.
    text = value.replace('Z', '+00:00')
    text = re.sub(r'\.(\d+)(?=[+-]\d{2}:\d{2}$)',
                  lambda match: '.' + match[1].ljust(6, '0'), text)
    return round(datetime.fromisoformat(text).timestamp() * 1000, 6)


def excerpt(text, limit):
    text = str(text or '')
    return text if len(text) <= limit else text[:limit - 1] + '…'


def chronological_fragments(entries, documents):
    """Use observed time; unknown fragments retain their source neighbours."""
    source_order = sorted(entries, key=lambda e: (int(e['messageIndex']), e['id']))
    positions = [{'id': e['id'], 'kind': 'tool',
                  'start_ms': epoch(documents[e['id']]['meta'].get('startedAt'))}
                 for e in source_order]
    merge_order(positions, [e['id'] for e in source_order])
    ranks = {p['id']: p['sequence'] for p in positions}
    return sorted(source_order, key=lambda e: ranks[e['id']])


def executions(items):
    """Exact duplicate identity+payload only; same command on a retry survives."""
    seen, unique, duplicates = {}, [], []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get('id'), str):
            raise ValueError('Command item requires its original id')
        if type(item.get('seq')) is not int or item['seq'] < 0:
            raise ValueError('Command item requires a nonnegative source sequence')
        if not isinstance(item.get('command'), str) or not isinstance(item.get('result'), str):
            raise ValueError('Command and result must be strings')
        key = canonical(item)
        if key in seen:
            duplicates.append({'kept_index': seen[key], 'duplicate_index': index})
        else:
            seen[key] = index
            unique.append((index, item))
    grouped = collections.OrderedDict()
    for index, item in sorted(unique, key=lambda pair: (pair[1]['seq'], pair[0])):
        # Only an explicit shared marker AND a returned source sequence support
        # merging commands into one execution. Never group on command text.
        shared = item.get('sharedDuration') is True and type(item.get('resultSeq')) is int
        key = ('shared', item['seq'], item['resultSeq']) if shared else ('item', index)
        grouped.setdefault(key, []).append((index, item))
    return list(grouped.values()), duplicates


def cross_fragment_repeats(entries, documents):
    """Flag repeated observations; local IDs do not justify cross-fragment deletion."""
    full_seen, core_seen, candidates = {}, {}, []
    fields = ('id', 'seq', 'resultSeq', 'subIndex', 'command', 'result')
    for entry in chronological_fragments(entries, documents):
        local_seen = set()
        for index, item in enumerate(documents[entry['id']]['items']):
            full = digest(canonical(item).encode())
            if full in local_seen:
                continue
            local_seen.add(full)
            core = digest(canonical({k: item.get(k) for k in fields}).encode())
            current = {'trace_id': entry['id'], 'message_index': str(entry['messageIndex']), 'item_index': index}
            previous = full_seen.get(full) or core_seen.get(core)
            if previous and previous['trace_id'] != entry['id']:
                candidates.append({'previous': previous, 'current': current,
                                   'basis': 'exact_record' if full in full_seen else 'same_local_key_command_result',
                                   'decision': 'retained; cross-fragment execution identity unconfirmed'})
            full_seen.setdefault(full, current)
            core_seen.setdefault(core, current)
    return candidates


def timing(group, origin):
    records = [(epoch(item.get('startedAt')), epoch(item.get('finishedAt')),
                item.get('durationMs')) for _, item in group]
    for start, end, elapsed in records:
        if elapsed is not None and (type(elapsed) not in (int, float)
                                    or not math.isfinite(elapsed) or elapsed < 0):
            return (None, None, None), 'invalid_duration'
        if start is not None and end is not None:
            if end < start or (elapsed is not None and abs(end - start - elapsed) > 1):
                return (None, None, None), 'inconsistent_timing'
    # Do not choose a member's timing if an alleged shared execution disagrees.
    if any(row != records[0] for row in records[1:]):
        return (None, None, None), 'conflicting_shared_timing'
    start, end, elapsed = records[0]
    relative = lambda value: round(value - origin, 6) if value is not None else None
    return (relative(start), relative(end), elapsed), None


def normalize_session(entries, documents, archive_sha, catalog_sha):
    entries = chronological_fragments(entries, documents)
    conversations = {entry['conversationId'] for entry in entries}
    if len(conversations) != 1:
        raise ValueError('A run cannot mix conversation IDs')
    conversation = next(iter(conversations))
    identity = digest(conversation.encode())[:32]
    run_id = 'dw-corpus-v' + VERSION.replace('.', '_') + '-' + archive_sha[:12] + '-' + identity
    query_id = 'doubao-session-' + identity
    stamps = []
    for entry in entries:
        document = documents[entry['id']]
        if not isinstance(document.get('meta'), dict) or not isinstance(document.get('items'), list):
            raise ValueError('Expected meta and items export')
        for record in [document['meta']] + document['items']:
            stamps.extend(epoch(record.get(key)) for key in ('startedAt', 'finishedAt')
                          if record.get(key) is not None)
    origin = min(stamps) if stamps else None
    coverage = {'tools': 'partial', 'model_requests': 'missing'}
    environment = unknown_environment()
    environment['notes'] = ('来源只包含 lark-cli 命令导出，未提供模型、沙箱、联网或环境身份。'
                            'env_id 是本会话的未知环境占位；query_id 是会话占位，不代表已匹配评测题。'
                            '各片段可能携带历史命令；标准记录数量不等于已证明独立的执行次数。')
    first = entries[0]
    trace = {
        'schema_version': 'trace-hunter/1.1',
        'run': {'id': run_id, 'query_id': query_id, 'env_id': 'unknown-doubao-' + identity,
                'title': excerpt(first.get('title') or documents[first['id']]['meta'].get('title') or '豆包工作会话', 4096),
                'query': excerpt('【来源摘要，完整 query 未提供】' + (first.get('promptSummary') or '未记录'), 4096),
                'harness': 'Doubao Work', 'model': '未知（来源未提供）', 'status': 'partial',
                'time_basis': '相对本会话最早可见时间；保留来源起止与 durationMs，共享执行只计一次。'
                              '阶段是 messageIndex 片段，未证明是完整用户轮次；前置间隔不能视为纯模型时间。'},
        'collector': {'name': 'doubao-command-corpus-adapter', 'version': VERSION},
        'environment': environment,
        'sources': [{'id': 'catalog', 'name': 'raw/builtin-traces/catalog.json', 'sha256': catalog_sha}],
        'coverage': coverage, 'phases': [], 'spans': [], 'links': [], 'evidence': []}
    audit = {'conversation_id': conversation, 'run_id': run_id, 'query_id': query_id,
             'env_id': trace['run']['env_id'], 'fragments': [], 'raw_items': 0,
             'duplicate_items_removed': 0, 'shared_executions': 0,
             'shared_commands': 0, 'executions': 0, 'changes': [],
             'cross_fragment_repeat_candidates': cross_fragment_repeats(entries, documents),
             'normalization': {'json_results_decoded': 0, 'source_status_corrections': 0,
                               'explicit_business_failure_records': 0, 'error_records': 0},
             'independent_execution_count': None,
             'ordering': {'policy': ORDER_POLICY, 'time_origin_epoch_ms': origin,
                          'fragment_order': [], 'record_sequence_changes': []}}
    item_spans = {}

    def note(ident, name, detail, source, span_ids=None):
        trace['evidence'].append({'id': ident, 'kind': 'note', 'name': name, 'status': 'observed',
                                  'detail': detail, 'span_ids': span_ids or [], 'source': source})

    for pi, entry in enumerate(entries):
        source_id = phase_id = 'fragment-' + str(pi)
        document = documents[entry['id']]
        items, meta = document['items'], document['meta']
        trace['sources'].append({'id': source_id, 'name': 'raw/' + entry['_member'],
                                'sha256': entry['_sha256']})
        source = lambda pointer: {'source_id': source_id, 'pointer': pointer}
        fragment = {k: entry[k] for k in ('id', 'datasetId', 'conversationId', 'messageIndex', 'sourceRow')}
        fragment.update(phase_id=phase_id, source_id=source_id, member=entry['_member'])
        audit['fragments'].append(fragment)
        audit['ordering']['fragment_order'].append({
            'phase_id': phase_id, 'source_trace_id': entry['id'], 'message_index': str(entry['messageIndex']),
            'started_at': meta.get('startedAt'),
            'basis': 'timestamp' if meta.get('startedAt') is not None else 'source_sequence'})
        note('identity-' + phase_id, '来源片段与摘要（完整消息未提供）',
             canonical({**fragment, 'catalog_entry': {k: v for k, v in entry.items()
                        if k not in ('_member', '_sha256', '_catalog_index')},
                        'promptSummary': entry.get('promptSummary'),
                        'responseSummary': entry.get('responseSummary')}),
             {'source_id': 'catalog', 'pointer': '/traces/' + str(entry['_catalog_index'])})
        note('metadata-' + phase_id, '来源片段完整元数据（计数为导出方口径）',
             canonical(meta), source('/meta'))
        groups, duplicates = executions(items)
        audit['raw_items'] += len(items)
        audit['duplicate_items_removed'] += len(duplicates)
        audit['executions'] += len(groups)
        phase_spans, source_to_span = [], {}
        for group in groups:
            index, item = group[0]
            sid = phase_id + '-call-' + str(index)
            shared = len(group) > 1
            start, end, elapsed = (None, None, None)
            (start, end, elapsed), issue = timing(group, origin)
            results, facts = normalized_execution(group)
            status = facts['normalized_status']
            name = 'lark-cli 批次（' + str(len(group)) + ' 条命令）' if shared else 'lark-cli ' + str(item.get('operation') or '未知操作')
            s = span(sid, name, phase=phase_id, operation='bash',
                     start_ms=start, end_ms=end, duration_ms=elapsed, status=status,
                     input={'commands': [x['command'] for _, x in group], 'shared_execution': True} if shared else {'command': item['command']},
                     output=results if shared else results[0],
                     sequence=len(trace['spans']), order_basis='source_sequence', source=source('/items/' + str(index)))
            trace['spans'].append(s)
            note('record-facts-' + sid, '调用身份、进程退出与业务返回依据', canonical(facts),
                 source('/items/' + str(index)), [sid])
            audit['normalization']['json_results_decoded'] += sum(f['result_encoding'] == 'json' for f in facts['items'])
            audit['normalization']['source_status_corrections'] += facts['status_corrected']
            audit['normalization']['explicit_business_failure_records'] += facts['explicit_business_failure']
            audit['normalization']['error_records'] += status == 'error'
            phase_spans.append(s)
            source_to_span.update((i, sid) for i, _ in group)
            if shared:
                audit['shared_executions'] += 1
                audit['shared_commands'] += len(group)
                detail = {'item_indices': [i for i, _ in group], 'seq': item['seq'],
                          'resultSeq': item['resultSeq'], 'rule': 'explicit sharedDuration; one execution, all commands retained'}
                note('shared-' + sid, '多条命令共享一次执行', canonical(detail), source('/items'), [sid])
            if issue:
                audit['changes'].append({'phase_id': phase_id, 'span_id': sid, 'reason': issue})
                note('timing-' + sid, '冲突计时保留在原始文件，标准计时置为未知', issue, source('/items/' + str(index)), [sid])
        if duplicates:
            for record in duplicates:
                record['span_id'] = source_to_span[record['kept_index']]
            note('duplicates-' + phase_id, '完全重复项去重索引', canonical(duplicates), source('/items'),
                 sorted({record['span_id'] for record in duplicates}))
            audit['changes'].append({'phase_id': phase_id, 'reason': 'exact_duplicate_items', 'records': duplicates})
        item_spans.update(((entry['id'], index), sid) for index, sid in source_to_span.items())
        rel = lambda value: round(epoch(value) - origin, 6) if value is not None else None
        start, end = rel(meta.get('startedAt')), rel(meta.get('finishedAt'))
        if start is not None and end is not None and end < start:
            raise ValueError('Invalid phase window')
        original_window = [start, end]
        observed_starts = [s['start_ms'] for s in phase_spans if s['start_ms'] is not None]
        observed_ends = [s['end_ms'] for s in phase_spans if s['end_ms'] is not None]
        if start is not None:
            start = min([start] + observed_starts)
        if end is not None:
            end = max([end] + observed_ends)
        if [start, end] != original_window:
            change = {'phase_id': phase_id, 'reason': 'window_extended_to_observed_calls',
                      'original_ms': original_window, 'normalized_ms': [start, end]}
            audit['changes'].append(change)
            note('window-' + phase_id, '展示窗口扩展至片段内已记录调用边界', canonical(change), source('/meta'))
        trace['phases'].append({'id': phase_id, 'name': '来源片段 messageIndex=' + str(entry['messageIndex']),
                                'purpose': 'task', 'start_ms': start, 'end_ms': end, 'coverage': coverage.copy()})
    by_trace = collections.defaultdict(list)
    for candidate in audit['cross_fragment_repeat_candidates']:
        candidate['previous']['span_id'] = item_spans[(candidate['previous']['trace_id'], candidate['previous']['item_index'])]
        candidate['current']['span_id'] = item_spans[(candidate['current']['trace_id'], candidate['current']['item_index'])]
        by_trace[candidate['current']['trace_id']].append(candidate)
    for pi, entry in enumerate(entries):
        candidates = by_trace[entry['id']]
        if candidates:
            note('cross-fragment-' + str(pi), '跨片段重复候选：保留记录，独立执行数未知', canonical(candidates),
                 {'source_id': 'fragment-' + str(pi), 'pointer': '/items'},
                 sorted({x['current']['span_id'] for x in candidates} | {x['previous']['span_id'] for x in candidates}))
    before = {s['id']: s['sequence'] for s in trace['spans']}
    merge_order(trace['spans'], [s['id'] for s in trace['spans']])
    trace['spans'].sort(key=lambda s: s['sequence'])
    audit['ordering']['record_sequence_changes'] = [
        {'span_id': s['id'], 'source_sequence': before[s['id']], 'chronological_sequence': s['sequence']}
        for s in trace['spans'] if s['sequence'] != before[s['id']]]
    note('ordering', '时间排序与缺失时间的来源位置', canonical(audit['ordering']),
         {'source_id': 'catalog', 'pointer': '/traces/' + str(entries[0]['_catalog_index'])})
    previews = {'run.title': (str(first.get('title') or documents[first['id']]['meta'].get('title') or '豆包工作会话'), 4096),
                'run.query': ('【来源摘要，完整 query 未提供】' + (first.get('promptSummary') or '未记录'), 4096),
                'catalog.case.title': (trace['run']['title'], 200),
                'catalog.case.description': (trace['run']['query'], 4000)}
    audit['header_previews'] = [{'target': key, 'source_length': len(text), 'limit': limit}
                               for key, (text, limit) in previews.items() if len(text) > limit]
    if audit['header_previews']:
        note('header-previews', '展示字段截短；完整内容保留在来源证据', canonical(audit['header_previews']),
             {'source_id': 'catalog', 'pointer': '/traces/' + str(first['_catalog_index'])})
    validate(trace)
    return trace, audit


def make_catalog(traces, archive_sha, declared_cases=None, context_sha=None):
    declared_cases = declared_cases or {}
    cases = {}
    for trace in traces:
        run = trace['run']; query_id = run['query_id']
        cases.setdefault(query_id, declared_cases.get(query_id) or {
            'query_id': query_id, 'title': excerpt(run['title'], 200),
            'description': excerpt(run['query'], 4000),
            'conversation': {'mode': 'unknown', 'turns': []}})
    catalog = {'schema_version': 'trace-hunter/catalog/1.0',
               'collection': {'id': 'doubao-work-corpus-' + archive_sha[:12] + '-normalized-v' + VERSION.replace('.', '_') + ('-ctx-' + context_sha[:12] if context_sha else ''),
                              'title': '豆包工作 · 批量命令轨迹', 'kind': 'task',
                              'description': '按 conversationId 组织的部分会话记录。query_id 为会话占位；片段不等于完整用户轮次，完整 query、模型与环境未提供。'},
               'cases': list(cases.values())}
    if declared_cases:
        catalog['collection']['description'] = '按来源会话组织的部分轨迹；题目和环境映射以随包 import-context 的用户声明为准，未映射身份保留占位。来源片段不证明完整用户轮次。'
    validate_catalog(catalog)
    return catalog
