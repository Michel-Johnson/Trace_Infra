"""Streaming import support for the Doubao message-level CSV export.

Each CSV row is one response sample. The embedded message list is a cumulative
runtime snapshot, so only explicitly identified current events are normalized.
Historical prompt context remains in the immutable raw CSV.
"""
import csv
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .corpus_import import canonical, decode, digest, epoch, excerpt, normalized_result
from .ordering import merge_order, skill_label
from .protocol import validate

ROOT = Path(__file__).resolve().parents[2]
FIELD_SPEC = decode((ROOT / 'contracts/imports/doubao-message-csv-fields-v1.json').read_bytes())
FORMAT = FIELD_SPEC['format']
VERSION = '1.0.0'
NULL_VALUES = set(FIELD_SPEC['null_values'])
FIELDS = FIELD_SPEC['fields']


def configure_csv():
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def is_null(value):
    return value in NULL_VALUES


def parsed_cell(value, kind):
    if is_null(value):
        return None
    if kind == 'string':
        return value
    if kind == 'integer':
        if not re.fullmatch(r'-?\d+', value):
            raise ValueError('expected integer')
        return int(value)
    if kind == 'number':
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('expected finite number')
        return result
    if kind == 'date':
        if not re.fullmatch(r'\d{8}', value):
            raise ValueError('expected YYYYMMDD')
        return value
    if kind == 'json':
        return decode(value)
    raise ValueError('unsupported field type')


def timestamp(value):
    if value is None or (isinstance(value, str) and is_null(value)):
        return None
    return epoch(value)


def duration(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return value


def content_value(value):
    """Unwrap plain text while preserving non-text multimodal structures."""
    if isinstance(value, dict) and value.get('type') == 'text' and isinstance(value.get('text'), str):
        return value['text']
    if isinstance(value, dict) and value.get('type') == 'multi_modal' and isinstance(value.get('multi_modal'), list):
        texts = []
        for item in value['multi_modal']:
            if not isinstance(item, dict):
                return value
            text = item.get('ContentType', {}).get('Text', {}).get('content')
            if not isinstance(text, str):
                return value
            texts.append(text)
        return '\n'.join(texts)
    return value


def safe_agent_id(value, prefix):
    if isinstance(value, str) and value:
        return prefix + '-' + digest(value.encode())[:16]
    return prefix


def message_operation(name):
    lowered = str(name).lower()
    if lowered in ('read', 'grep', 'glob'):
        return 'read'
    if lowered in ('write', 'edit'):
        return 'write'
    if lowered == 'bash':
        return 'bash'
    if lowered == 'skill':
        return 'skill'
    if lowered == 'wait' or lowered == 'notifyhuman' or lowered.startswith('interaction'):
        return 'human'
    return 'other'


def iter_agents(main):
    """Yield the main agent and observed organizer/subagent trees."""
    if not isinstance(main, dict):
        return
    yield 'main', main, '/message_info_list_final/mainagent'

    def descendants(agent, label, pointer):
        yield label, agent, pointer
        for index, child in enumerate(agent.get('subagents') or []):
            if isinstance(child, dict):
                child_label = safe_agent_id(child.get('id'), 'subagent')
                yield from descendants(child, child_label, pointer + f'/subagents/{index}')

    organizers = main.get('organizers')
    if isinstance(organizers, list):
        for index, agent in enumerate(organizers):
            if isinstance(agent, dict):
                label = safe_agent_id(agent.get('id'), 'organizer')
                yield from descendants(agent, label, f'/message_info_list_final/mainagent/organizers/{index}')


def iter_messages(agent, pointer):
    source_order = 0
    for block_index, block in enumerate(agent.get('messages') or []):
        if not isinstance(block, dict) or not isinstance(block.get('list'), list):
            continue
        for message_index, message in enumerate(block['list']):
            if isinstance(message, dict):
                yield source_order, message, pointer + f'/messages/{block_index}/list/{message_index}'
            source_order += 1


def current_message(message):
    return 'history_session' not in message and 'history_round' not in message


def usage_value(value):
    if not isinstance(value, dict):
        return None
    mapped = {
        'input_tokens': value.get('input_tokens'),
        'output_tokens': value.get('output_tokens'),
        'cache_read_tokens': value.get('cached_tokens'),
        'cache_write_tokens': None,
        'thinking_tokens': None,
    }
    if any(type(v) is not int or v < 0 for v in mapped.values() if v is not None):
        return None
    if (mapped['input_tokens'] is not None and mapped['cache_read_tokens'] is not None
            and mapped['cache_read_tokens'] > mapped['input_tokens']):
        return None
    return mapped


def source_status(content):
    value = content_value(content)
    _, facts = normalized_result(value)
    business = facts['business_ok']
    process_code = None
    if isinstance(value, str):
        match = re.match(r"\A(?:Command .*?|'?Process)'? exited with code (-?\d+)\.(?:\s|$)", value, re.S)
        if match:
            process_code = int(match.group(1))
    if process_code not in (None, 0) or False in business:
        return 'error', process_code, business
    if business and all(business):
        return 'ok', process_code, business
    return 'unknown', process_code, business


def observed_events(document):
    """Select current, identified events and retain explicit discrepancies."""
    main = document.get('mainagent') if isinstance(document, dict) else None
    models = {}
    calls = {}
    results = {}
    aliases = Counter()
    conflicts = []
    conflict_count = 0
    agent_summaries = []
    order_offset = 0
    for agent_id, agent, pointer in iter_agents(main):
        model_name = agent.get('model_name') if isinstance(agent.get('model_name'), str) else None
        agent_summaries.append({
            'agent_id': agent_id,
            'source_agent_id': agent.get('id'),
            'agent_type': agent.get('agent_type'),
            'model_name': model_name,
            'model_type': agent.get('model_type'),
            'protocol': agent.get('protocol'),
            'aggregate_token_usage': agent.get('token_usage'),
        })
        for local_order, message, message_pointer in iter_messages(agent, pointer):
            source_order = order_offset + local_order
            if not current_message(message):
                continue
            role = message.get('role')
            if role == 'assistant' and isinstance(message.get('token_usage'), dict):
                key = digest(canonical({
                    'agent_id': agent_id,
                    'start_time': message.get('start_time'),
                    'duration_ms': message.get('duration_ms'),
                    'token_usage': message.get('token_usage'),
                    'cache_hit_rate': message.get('cache_hit_rate'),
                    'content': message.get('content'),
                }).encode())
                if key in models:
                    aliases['model'] += 1
                    models[key]['aliases'].append(message_pointer)
                else:
                    models[key] = {'key': key, 'agent_id': agent_id, 'model_name': model_name,
                                   'message': message, 'pointer': message_pointer,
                                   'source_order': source_order, 'aliases': []}
            elif role in ('tool_call', 'tool_result') and isinstance(message.get('tool_call_id'), str):
                key = (agent_id, message['tool_call_id'])
                target = calls if role == 'tool_call' else results
                if key in target:
                    aliases[role] += 1
                    previous = target[key]
                    if canonical(previous['message'].get('content')) != canonical(message.get('content')):
                        conflict_count += 1
                        if len(conflicts) < 20:
                            conflicts.append({'kind': role, 'agent_id': agent_id,
                                              'tool_call_id_hash': digest(message['tool_call_id'].encode())[:16]})
                    previous_content = canonical(previous['message'].get('content'))
                    current_content = canonical(message.get('content'))
                    previous_score = ('start_time' in previous['message'], len(previous_content),
                                      previous['source_order'])
                    current_score = ('start_time' in message, len(current_content), source_order)
                    if current_score > previous_score:
                        target[key] = {'agent_id': agent_id, 'message': message,
                                       'pointer': message_pointer, 'source_order': source_order}
                else:
                    target[key] = {'agent_id': agent_id, 'message': message,
                                   'pointer': message_pointer, 'source_order': source_order}
        order_offset += sum(len(b.get('list', [])) for b in agent.get('messages', []) if isinstance(b, dict)) + 1
    meta = document.get('meta') if isinstance(document, dict) else {}
    aggregate = meta.get('token_usage') if isinstance(meta, dict) else None
    summed = Counter()
    for record in models.values():
        value = record['message'].get('token_usage')
        if isinstance(value, dict):
            for key in ('input_tokens', 'output_tokens', 'cached_tokens'):
                if type(value.get(key)) is int:
                    summed[key] += value[key]
    difference = {}
    if isinstance(aggregate, dict):
        for key in ('input_tokens', 'output_tokens', 'cached_tokens'):
            value = aggregate.get(key)
            difference[key] = value - summed[key] if type(value) is int else None
    stats = {
        'declared_tool_calls': meta.get('tool_call_cnt') if isinstance(meta, dict) else None,
        'observed_tool_calls': len(calls),
        'observed_tool_results': len(results),
        'paired_tool_calls': len(set(calls) & set(results)),
        'declared_model_calls': meta.get('model_call_cnt') if isinstance(meta, dict) else None,
        'observed_model_calls': len(models),
        'model_snapshot_duplicates_removed': aliases['model'],
        'tool_call_aliases_removed': aliases['tool_call'],
        'tool_result_aliases_removed': aliases['tool_result'],
        'aggregate_token_usage': aggregate,
        'observed_model_token_usage': dict(summed),
        'unattributed_token_difference': difference,
        'identity_conflict_count': conflict_count,
        'identity_conflicts': conflicts,
        'agents': agent_summaries,
    }
    return list(models.values()), calls, results, stats


def coverage_state(declared, observed, paired=None, usage_complete=True):
    if declared == 0 and observed == 0:
        return 'complete'
    if observed == 0:
        return 'missing'
    if declared == observed and (paired is None or paired == observed) and usage_complete:
        return 'complete'
    return 'partial'


def row_prompt(row):
    for key in ('masking_parent_content', 'prompt'):
        value = row.get(key)
        if isinstance(value, str) and not is_null(value):
            return value, key
    return None, None


def row_response(row):
    for key in ('masking_message_content', 'response'):
        value = row.get(key)
        if isinstance(value, str) and not is_null(value):
            return value, key
    return None, None


def normalize_record(row, row_number, source_sha, source_name='source.csv', events=None, document=None):
    document = document if document is not None else decode(row['message_info_list_final'])
    models, calls, results, stats = events or observed_events(document)
    meta = document['meta']
    prompt, prompt_field = row_prompt(row)
    response, response_field = row_response(row)
    source_identity = row['enc_bot_message_id']
    run_identity = digest(source_identity.encode())[:32]
    run_id = f'dw-message-v{VERSION.replace(".", "_")}-{source_sha[:12]}-{run_identity}'
    query_id = ('doubao-query-' + digest(prompt.encode())[:32] if prompt is not None
                else 'doubao-query-missing-' + run_identity)
    environment_facts = {
        'platform': row['platform'],
        'local_computer_mode': parsed_cell(row['is_local_computer_mode'], 'integer'),
        'app_id': parsed_cell(row['app_id'], 'integer'),
        'mode_type': parsed_cell(row['mode_type'], 'integer'),
        'app_version': None if is_null(row['app_version']) else row['app_version'],
    }
    env_id = 'doubao-observed-' + digest(canonical(environment_facts).encode())[:24]
    source_id = 'source-csv'
    source = lambda pointer: {'source_id': source_id, 'pointer': f'/rows/{row_number}' + pointer}

    known_times = []
    for value in (meta.get('start_time'), meta.get('end_time')):
        try:
            observed = timestamp(value)
        except (ValueError, OverflowError):
            observed = None
        if observed is not None:
            known_times.append(observed)
    for record in models + list(calls.values()) + list(results.values()):
        try:
            observed = timestamp(record['message'].get('start_time'))
        except (ValueError, OverflowError):
            observed = None
        if observed is not None:
            known_times.append(observed)
    origin = min(known_times) if known_times else None

    def relative(value):
        try:
            absolute = timestamp(value)
        except (ValueError, OverflowError):
            return None
        return round(absolute - origin, 6) if absolute is not None and origin is not None else None

    aggregate = stats['aggregate_token_usage']
    differences = stats['unattributed_token_difference']
    usage_complete = (isinstance(aggregate, dict)
                      and all(differences.get(k) == 0 for k in ('input_tokens', 'output_tokens', 'cached_tokens')))
    model_coverage = coverage_state(stats['declared_model_calls'], stats['observed_model_calls'],
                                    usage_complete=usage_complete)
    tool_coverage = coverage_state(stats['declared_tool_calls'], stats['observed_tool_calls'],
                                   stats['paired_tool_calls'])
    coverage = {'tools': tool_coverage, 'model_requests': model_coverage}

    status = 'failed' if meta.get('status') == 'error' else 'completed'
    if status == 'completed' and (row.get('is_interrupted') == '1' or response is None):
        status = 'partial'
    title_source = next((line.strip() for line in (prompt or '').splitlines() if line.strip()), '')
    title = excerpt(title_source or f'Doubao response row {row_number}', 200)
    query = excerpt(prompt or '【用户输入未提供】', 4096)
    start = relative(meta.get('start_time'))
    end = relative(meta.get('end_time'))
    observed_at = meta.get('start_time') if start is not None else None
    trace = {
        'schema_version': 'trace-hunter/1.1',
        'run': {'id': run_id, 'query_id': query_id, 'env_id': env_id, 'title': title,
                'query': query, 'harness': 'Doubao Work', 'model': row['main_agent_model_name'],
                'status': status,
                'time_basis': ('Each CSV row is one response sample. Source start_time and duration_ms are retained; '
                               'cumulative history is not re-imported. Aggregate usage that cannot be assigned to '
                               'an observed request remains evidence and model coverage is partial.')},
        'collector': {'name': 'doubao-message-csv-adapter', 'version': VERSION},
        'environment': {'isolation': 'unknown', 'network_access': 'unknown',
                        'observed_at': observed_at, 'snapshot_id': None,
                        'tool_versions': {'Doubao Work': environment_facts['app_version']},
                        'notes': 'Observed source fields: ' + canonical(environment_facts)},
        'sources': [{'id': source_id, 'name': 'raw/' + source_name, 'sha256': source_sha}],
        'coverage': coverage,
        'phases': [{'id': 'response', 'name': '消息回复 ' + row['message_index'], 'purpose': 'task',
                    'start_ms': start, 'end_ms': end, 'coverage': coverage.copy()}],
        'spans': [], 'links': [], 'evidence': [],
    }
    internal = {}
    for record in models:
        message = record['message']
        sid = 'model-' + record['key'][:32]
        value = usage_value(message.get('token_usage'))
        model_name = record['model_name'] or row['main_agent_model_name']
        trace['spans'].append({
            'id': sid, 'kind': 'model', 'name': '模型请求 · ' + excerpt(model_name, 200),
            'operation': 'other', 'agent_id': record['agent_id'], 'parent_id': None,
            'request_id': 'observed-' + record['key'][:32], 'attempt': 1, 'phase_id': 'response',
            'start_ms': relative(message.get('start_time')), 'end_ms': None,
            'duration_ms': duration(message.get('duration_ms')), 'status': 'unknown',
            'input': None, 'output': content_value(message.get('content')), 'usage': value,
            'source': source(record['pointer']),
        })
        internal[sid] = {'source_order': record['source_order'], 'agent_id': record['agent_id'],
                         'source_pointer': record['pointer'], 'aliases': record['aliases']}

    for (agent_id, tool_call_id), call in calls.items():
        result = results.get((agent_id, tool_call_id))
        call_message = call['message']
        result_message = result['message'] if result else {}
        call_duration = duration(call_message.get('duration_ms'))
        result_duration = duration(result_message.get('duration_ms'))
        timing_issue = call_duration is not None and result_duration is not None and call_duration != result_duration
        elapsed = None if timing_issue else call_duration if call_duration is not None else result_duration
        start_ms = relative(call_message.get('start_time'))
        end_ms = relative(result_message.get('start_time'))
        if start_ms is not None and end_ms is not None and end_ms < start_ms:
            end_ms = None
            timing_issue = True
        output = content_value(result_message.get('content')) if result else None
        normalized_status, process_code, business = source_status(output)
        key_hash = digest((agent_id + '\0' + tool_call_id).encode())[:32]
        sid = 'tool-' + key_hash
        tool_name = call_message.get('tool_name')
        if not isinstance(tool_name, str) or not tool_name:
            tool_name = result_message.get('tool_name') or 'unknown'
        trace['spans'].append({
            'id': sid, 'kind': 'tool', 'name': excerpt(tool_name, 200),
            'operation': message_operation(tool_name), 'agent_id': agent_id, 'parent_id': None,
            'request_id': tool_call_id, 'attempt': 1, 'phase_id': 'response',
            'start_ms': start_ms, 'end_ms': end_ms, 'duration_ms': elapsed,
            'status': normalized_status, 'input': call_message.get('content'), 'output': output,
            'usage': None, 'source': source(call['pointer']),
        })
        internal[sid] = {'source_order': call['source_order'], 'agent_id': agent_id,
                         'source_pointer': call['pointer'],
                         'result_pointer': result['pointer'] if result else None,
                         'timing_issue': timing_issue, 'process_exit_code': process_code,
                         'business_ok': business}

    source_order = [sid for sid, _ in sorted(internal.items(), key=lambda pair: pair[1]['source_order'])]
    ordering = [{'id': item['id'], 'kind': 'tool', 'start_ms': item['start_ms']}
                for item in trace['spans']]
    merge_order(ordering, source_order)
    ranks = {item['id']: (item['sequence'], item['order_basis']) for item in ordering}
    for span_item in trace['spans']:
        span_item['sequence'], span_item['order_basis'] = ranks[span_item['id']]
        label = skill_label(span_item)
        if label:
            span_item['skill'] = label
    trace['spans'].sort(key=lambda item: item['sequence'])
    previous_models = defaultdict(list)
    for span_item in trace['spans']:
        details = internal[span_item['id']]
        if span_item['kind'] == 'model':
            previous_models[span_item['agent_id']].append(span_item['id'])
        elif previous_models[span_item['agent_id']]:
            trace['links'].append({'from': previous_models[span_item['agent_id']][-1],
                                   'to': span_item['id'], 'type': 'invokes'})

    span_ends = []
    for span_item in trace['spans']:
        if span_item['end_ms'] is not None:
            span_ends.append(span_item['end_ms'])
        elif span_item['start_ms'] is not None and span_item['duration_ms'] is not None:
            span_ends.append(span_item['start_ms'] + span_item['duration_ms'])
    span_starts = [item['start_ms'] for item in trace['spans'] if item['start_ms'] is not None]
    if span_starts:
        trace['phases'][0]['start_ms'] = min([x for x in [start, *span_starts] if x is not None])
    if span_ends:
        trace['phases'][0]['end_ms'] = max([x for x in [end, *span_ends] if x is not None])

    identity_detail = {
        'source_row': row_number,
        'date': row['date'],
        'conversation_id': row['enc_conversation_id'],
        'section_id': row['enc_section_id'],
        'message_id': row['enc_message_id'],
        'bot_message_id': row['enc_bot_message_id'],
        'log_id': row['enc_log_id'],
        'message_index': row['message_index'],
        'prompt_field': prompt_field,
        'response_field': response_field,
        'source_status': meta.get('status'),
        'interrupted': row.get('is_interrupted'),
        'event_accounting': stats,
        'tool_result_sources': [
            {'span_id': span_id, 'call_pointer': facts['source_pointer'],
             'result_pointer': facts['result_pointer']}
            for span_id, facts in internal.items() if 'result_pointer' in facts
        ],
        'model_snapshot_aliases': [
            {'span_id': span_id, 'kept_pointer': facts['source_pointer'],
             'alias_pointers': facts['aliases']}
            for span_id, facts in internal.items() if facts.get('aliases')
        ],
    }
    trace['evidence'].append({
        'id': 'source-row', 'kind': 'note', 'name': 'CSV 行身份与事件覆盖',
        'status': 'observed', 'detail': canonical(identity_detail), 'span_ids': [],
        'source': source(''),
    })
    if response is not None:
        trace['evidence'].append({
            'id': 'final-response', 'kind': 'artifact', 'name': '该样本的最终回复',
            'status': 'observed', 'detail': response, 'span_ids': [],
            'source': source('/' + response_field),
        })
    artifacts = document.get('artifacts')
    if isinstance(artifacts, dict) and any(artifacts.get(key) for key in ('products', 'uploaded_attachments')):
        artifact_detail = canonical(artifacts)
        if len(artifact_detail) > 200000:
            artifact_detail = canonical({
                'products': len(artifacts.get('products') or []),
                'uploaded_attachments': len(artifacts.get('uploaded_attachments') or []),
                'detail_sha256': digest(artifact_detail.encode()),
                'detail': 'Full artifact metadata remains in the raw CSV.',
            })
        trace['evidence'].append({
            'id': 'source-artifacts', 'kind': 'artifact', 'name': '来源产物与附件',
            'status': 'observed', 'detail': artifact_detail, 'span_ids': [],
            'source': source('/message_info_list_final/artifacts'),
        })
    problems = {
        'model_snapshot_duplicates_removed': stats['model_snapshot_duplicates_removed'],
        'unpaired_tool_calls': stats['observed_tool_calls'] - stats['paired_tool_calls'],
        'unpaired_tool_results': stats['observed_tool_results'] - stats['paired_tool_calls'],
        'identity_conflict_count': stats['identity_conflict_count'],
        'identity_conflicts': stats['identity_conflicts'],
        'tool_timing_conflicts': sum(bool(item.get('timing_issue')) for item in internal.values()),
    }
    if any(value for value in problems.values()):
        trace['evidence'].append({
            'id': 'normalization-notes', 'kind': 'note', 'name': '累计快照去重与不完整事件',
            'status': 'observed', 'detail': canonical(problems),
            'span_ids': [], 'source': source('/message_info_list_final/mainagent/messages'),
        })
    validate(trace)
    index = {
        'source_row': row_number, 'run_id': run_id, 'query_id': query_id, 'env_id': env_id,
        'conversation_id': row['enc_conversation_id'], 'section_id': row['enc_section_id'],
        'message_id': row['enc_message_id'], 'bot_message_id': row['enc_bot_message_id'],
        'message_index': int(row['message_index']), 'prompt_sha256': digest((prompt or '').encode()),
        'prompt_present': prompt is not None, 'response_present': response is not None,
        'model_spans': sum(item['kind'] == 'model' for item in trace['spans']),
        'tool_spans': sum(item['kind'] == 'tool' for item in trace['spans']),
        'coverage': coverage, 'event_accounting': stats,
    }
    case = {
        'query_id': query_id, 'title': excerpt(title, 200),
        'description': excerpt(prompt or '用户输入未提供。', 4000),
        'conversation': {'mode': 'single_turn' if prompt is not None else 'unknown',
                         'turns': [] if prompt is None else [{'id': 'turn-1', 'prompt': excerpt(prompt, 100000)}]},
    }
    return trace, index, case


class CsvAuditor:
    def __init__(self, header):
        self.header = header
        self.rows = 0
        self.metrics = {name: {'present': 0, 'null': 0, 'empty': 0,
                               'types': Counter(), 'max_string_length': 0}
                        for name in FIELDS}
        self.issue_counts = Counter()
        self.issue_rows = defaultdict(list)
        self.bot_ids = set()
        self.message_ids = set()
        self.conversation_messages = set()
        self.conversations = set()
        self.query_hashes = set()
        self.totals = Counter()
        self.nested = defaultdict(Counter)
        self.unknown_columns = [name for name in header if name not in FIELDS]
        for name, spec in FIELDS.items():
            if spec['required'] and name not in header:
                self.issue('missing_required_column', 0, 'error', name)
        if len(header) != len(set(header)):
            self.issue('duplicate_column_name', 0, 'error', 'header')

    def issue(self, code, row, severity='warning', field=None):
        key = (severity, code, field or '')
        self.issue_counts[key] += 1
        if len(self.issue_rows[key]) < 10:
            self.issue_rows[key].append(row)

    def observe(self, row_number, row, document, events):
        self.rows += 1
        for name, spec in FIELDS.items():
            if name not in row:
                continue
            value = row[name]
            metric = self.metrics[name]
            if value is None:
                self.issue('missing_column_value', row_number, 'error', name)
                continue
            metric['present'] += 1
            metric['max_string_length'] = max(metric['max_string_length'], len(value))
            metric['empty'] += value == ''
            if is_null(value):
                metric['null'] += 1
                metric['types']['null'] += 1
                if not spec['nullable']:
                    self.issue('null_not_allowed', row_number, 'error', name)
                continue
            try:
                parsed = document if name == 'message_info_list_final' else parsed_cell(value, spec['type'])
                metric['types'][type(parsed).__name__] += 1
            except (ValueError, TypeError):
                metric['types']['invalid'] += 1
                self.issue('invalid_' + spec['type'], row_number, 'error', name)
        bot_id = row.get('enc_bot_message_id')
        if bot_id in self.bot_ids:
            self.issue('duplicate_bot_message_id', row_number, 'error', 'enc_bot_message_id')
        self.bot_ids.add(bot_id)
        message_id = row.get('enc_message_id')
        if message_id in self.message_ids:
            self.issue('duplicate_message_id', row_number, 'error', 'enc_message_id')
        self.message_ids.add(message_id)
        pair = (row.get('enc_conversation_id'), row.get('message_index'))
        if pair in self.conversation_messages:
            self.issue('duplicate_conversation_message_index', row_number, 'error', 'message_index')
        self.conversation_messages.add(pair)
        self.conversations.add(row.get('enc_conversation_id'))
        prompt, _ = row_prompt(row)
        if prompt is None:
            self.issue('missing_prompt', row_number, 'warning', 'prompt')
            self.query_hashes.add('missing:' + str(bot_id))
        else:
            self.query_hashes.add(digest(prompt.encode()))
        if row_response(row)[0] is None:
            self.issue('missing_response', row_number, 'warning', 'response')
        if row.get('prompt') != row.get('masking_parent_content'):
            self.issue('masked_prompt_differs', row_number, 'warning', 'masking_parent_content')
        if row.get('response') != row.get('masking_message_content'):
            self.issue('masked_response_differs', row_number, 'warning', 'masking_message_content')
        if not isinstance(document, dict):
            self.issue('message_info_not_object', row_number, 'error', 'message_info_list_final')
            return
        self.nested['top_level'][','.join(sorted(document))] += 1
        meta = document.get('meta')
        main = document.get('mainagent')
        artifacts = document.get('artifacts')
        if not isinstance(meta, dict):
            self.issue('meta_not_object', row_number, 'error', 'message_info_list_final')
            return
        if not isinstance(main, dict):
            self.issue('mainagent_not_object', row_number, 'error', 'message_info_list_final')
            return
        if not isinstance(artifacts, dict):
            self.issue('artifacts_not_object', row_number, 'error', 'message_info_list_final')
        else:
            self.totals['artifact_products'] += len(artifacts.get('products') or [])
            self.totals['uploaded_attachments'] += len(artifacts.get('uploaded_attachments') or [])
        self.nested['meta_keys'][','.join(sorted(meta))] += 1
        self.nested['mainagent_keys'][','.join(sorted(main))] += 1
        self.nested['meta_status'][str(meta.get('status'))] += 1
        self.nested['meta_version'][str(meta.get('version'))] += 1
        if meta.get('status') not in ('completed', 'error'):
            self.issue('unknown_meta_status', row_number, 'warning', 'message_info_list_final')
        for name in ('start_time', 'end_time'):
            try:
                timestamp(meta.get(name))
            except (ValueError, OverflowError):
                self.issue('invalid_meta_timestamp', row_number, 'error', name)
        for name in ('token_usage',):
            if usage_value(meta.get(name)) is None:
                self.issue('invalid_aggregate_usage', row_number, 'error', name)
        if meta.get('token_usage') != main.get('token_usage'):
            self.issue('meta_main_usage_mismatch', row_number, 'warning', 'token_usage')
        if row.get('main_agent_model_name') != str(main.get('model_name')):
            self.issue('main_model_mismatch', row_number, 'error', 'main_agent_model_name')
        models, calls, results, stats = events
        self.totals['observed_agents'] += len(stats['agents'])
        declared_tools = stats['declared_tool_calls']
        declared_models = stats['declared_model_calls']
        if declared_tools != stats['observed_tool_calls'] or stats['paired_tool_calls'] != stats['observed_tool_calls']:
            self.issue('tool_event_coverage_partial', row_number)
        if declared_models != stats['observed_model_calls']:
            self.issue('model_event_count_partial', row_number)
        differences = stats['unattributed_token_difference']
        if not all(differences.get(key) == 0 for key in ('input_tokens', 'output_tokens', 'cached_tokens')):
            self.issue('model_usage_not_fully_attributed', row_number)
        if stats['identity_conflict_count']:
            self.issue('tool_identity_snapshot_conflict', row_number)
        self.totals.update(
            source_rows=1,
            declared_tool_calls=declared_tools if type(declared_tools) is int else 0,
            observed_tool_calls=len(calls),
            paired_tool_calls=stats['paired_tool_calls'],
            observed_tool_results=len(results),
            declared_model_calls=declared_models if type(declared_models) is int else 0,
            observed_model_calls=len(models),
            model_snapshot_duplicates_removed=stats['model_snapshot_duplicates_removed'],
        )

    def report(self):
        fields = []
        for name, spec in FIELDS.items():
            metric = self.metrics[name]
            fields.append({'field': name, **spec, **metric, 'types': dict(metric['types']),
                           'missing': self.rows - metric['present'], 'records_in_scope': self.rows})
        issues = [{'severity': severity, 'code': code, 'field': field or None,
                   'count': count, 'example_rows': self.issue_rows[(severity, code, field)]}
                  for (severity, code, field), count in sorted(self.issue_counts.items())]
        return {
            'schema_version': 'trace-hunter/source-audit/1.0',
            'format': FORMAT,
            'scope': {'source_rows': self.rows, 'runs': self.rows,
                      'conversations': len(self.conversations), 'query_groups': len(self.query_hashes)},
            'fields': fields,
            'unmapped_fields': {'csv': {name: self.rows for name in self.unknown_columns}},
            'nested_shapes': {name: dict(values) for name, values in self.nested.items()},
            'counts': dict(self.totals),
            'issues': issues,
            'errors': sum(count for (severity, _, _), count in self.issue_counts.items() if severity == 'error'),
            'warnings': sum(count for (severity, _, _), count in self.issue_counts.items() if severity == 'warning'),
        }


def inspect_csv(path, on_record=None):
    configure_csv()
    path = Path(path)
    with path.open('r', encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise ValueError('CSV header is required')
        auditor = CsvAuditor(reader.fieldnames)
        header_valid = not any(severity == 'error' for severity, _, _ in auditor.issue_counts)
        for row_number, row in enumerate(reader, 1):
            if None in row:
                auditor.issue('row_column_count_mismatch', row_number, 'error')
                continue
            try:
                document = decode(row['message_info_list_final'])
            except (KeyError, ValueError, TypeError):
                auditor.issue('invalid_message_info_json', row_number, 'error',
                              'message_info_list_final')
                continue
            events = observed_events(document)
            errors_before = sum(count for (severity, _, _), count in auditor.issue_counts.items()
                                if severity == 'error')
            auditor.observe(row_number, row, document, events)
            errors_after = sum(count for (severity, _, _), count in auditor.issue_counts.items()
                               if severity == 'error')
            if on_record is not None and header_valid and errors_after == errors_before:
                on_record(row_number, row, document, events)
    return auditor.report()


def require_valid_csv(report):
    errors = [item for item in report['issues'] if item['severity'] == 'error']
    if errors:
        raise ValueError('CSV source validation failed: ' + '; '.join(
            f'{item["code"]} ({item["count"]})' for item in errors[:10]))
