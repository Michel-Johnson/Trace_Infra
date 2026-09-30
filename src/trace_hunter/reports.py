"""Three separately described analysis outputs, produced only on explicit request."""
from collections import Counter
from decimal import Decimal
import math

from .timeline import select_phases
from .analysis import union

VERSION = 'three-analyses/1.0.1'
RATE_KEYS = ('input_per_million', 'output_per_million', 'cache_read_per_million', 'cache_write_per_million')


def validate_prices(book):
    if book is None:
        return None
    if not isinstance(book, dict) or not isinstance(book.get('version'), str) or not book['version'] or not isinstance(book.get('entries'), list):
        raise ValueError('价格配置需要 version 和 entries')
    seen = set()
    for entry in book['entries']:
        if not isinstance(entry, dict) or any(not isinstance(entry.get(k), str) or not entry[k] for k in ('harness', 'model', 'currency', 'source')):
            raise ValueError('每条价格需要 harness、model、currency、source')
        key = (entry['harness'], entry['model'])
        if key in seen:
            raise ValueError('同一环境与模型存在重复价格')
        seen.add(key)
        for field in RATE_KEYS:
            n = entry.get(field)
            if n is not None and (isinstance(n, bool) or not isinstance(n, (float, int)) or not math.isfinite(n) or n < 0):
                raise ValueError('价格必须是非负有限数，未知可为 null')
    return book


def cost_report(trace, base, prices):
    tokens = base['tokens']
    entry = next((e for e in (prices or {}).get('entries', []) if
                  e['harness'] == trace['run']['harness'] and e['model'] == trace['run']['model']), None)
    result = {'tokens': tokens, 'request_count': base['model_request_count'],
              'pricing_status': 'missing_price', 'amount': None, 'observed_amount': None,
              'currency': entry['currency'] if entry else None, 'price': entry,
              'price_version': prices['version'] if prices else None}
    if entry is None:
        return result
    required = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens')
    if any(tokens[k] is None or tokens['coverage'][k]['recorded'] != tokens['coverage'][k]['requests'] for k in required):
        result['pricing_status'] = 'missing_usage'
        return result
    counts = (tokens['input_tokens'] - tokens['cache_read_tokens'] - tokens['cache_write_tokens'],
              tokens['output_tokens'], tokens['cache_read_tokens'], tokens['cache_write_tokens'])
    if any(count < 0 for count in counts):
        result['pricing_status'] = 'inconsistent_usage'
        return result
    if any(count and entry.get(field) is None for field, count in zip(RATE_KEYS, counts)):
        result['pricing_status'] = 'incomplete_price'
        return result
    amount = sum(Decimal(str(entry.get(field) or 0)) * count for field, count in zip(RATE_KEYS, counts)) / Decimal(1000000)
    result['observed_amount'] = str(amount)
    result['amount'] = str(amount) if tokens['complete'] else None
    result['pricing_status'] = 'estimated' if tokens['complete'] else 'partial_estimate'
    return result


def trajectory_report(trace, base):
    rows = base['rows']
    names = Counter(r['name'] for r in rows)
    skills = {}
    for row in rows:
        if not row.get('skill'):
            continue
        key = (row['skill']['name'], row['skill']['action'])
        if key not in skills:
            skills[key] = {**row['skill'], 'count': 0, 'span_ids': []}
        skills[key]['count'] += 1
        skills[key]['span_ids'].append(row['span_id'])
    return {'tools': [{'name': name, 'count': count} for name, count in names.most_common()],
            'skills': list(skills.values()),
            'errors': [r['span_id'] for r in rows if r['status'] == 'error'],
            'unknown_status_count': sum(r['status'] == 'unknown' for r in rows),
            'llm_review': {'status': 'not_requested', 'findings': []}}


def time_report(trace, base):
    phases = select_phases(trace, base['scope'])
    phase_ids = {p['id'] for p in phases}
    bounded = all(p['start_ms'] is not None and p['end_ms'] is not None for p in phases)
    low = min(p['start_ms'] for p in phases) if bounded else None
    high = max(p['end_ms'] for p in phases) if bounded else None
    intervals = {'model': [], 'automatic_tool': [], 'interaction': []}
    interaction_count = 0
    for span in trace['spans']:
        if span['phase_id'] not in phase_ids:
            continue
        if span['kind'] in ('tool', 'wait') and span['operation'] == 'human':
            group = 'interaction'
            interaction_count += 1
        elif span['kind'] == 'tool':
            group = 'automatic_tool'
        elif span['kind'] == 'model':
            group = 'model'
        else:
            continue
        if span['start_ms'] is None or span['end_ms'] is None:
            continue
        a, b = span['start_ms'], span['end_ms']
        if bounded:
            a, b = max(a, low), min(b, high)
        if b >= a:
            intervals[group].append((a, b))
    return {key: base[key] for key in ('wall_ms', 'tool_time_union_ms', 'tool_time_sum_ms', 'other_time_ms', 'tool_time_ratio', 'tool_time_known', 'tool_count', 'time_basis')} | {
        'model_time_union_ms': union(intervals['model']) if intervals['model'] else None,
        'automatic_tool_time_union_ms': union(intervals['automatic_tool']) if intervals['automatic_tool'] else None,
        'interaction_time_union_ms': union(intervals['interaction']) if intervals['interaction'] else None,
        'interaction_count': interaction_count,
        'interaction_time_known': len(intervals['interaction']),
        'rows': base['rows'],
        'note': '总耗时包含交互；交互用时按已记录区间统计，不等于纯等待时间。模型、自动工具与交互区间可能重叠，不能直接相加；前置间隔不是纯模型生成时间。'}


def compose(trace, base, prices=None):
    validate_prices(prices)
    return {'version': VERSION, 'analyzer_version': base['analyzer_version'], 'scope': base['scope'],
            'cost': cost_report(trace, base, prices),
            'trajectory': trajectory_report(trace, base),
            'time': time_report(trace, base)}
