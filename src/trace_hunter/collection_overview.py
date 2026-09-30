"""Compact collection facts and explicit classification dispatch over immutable imports.

Counts are captured tool records, not inferred independent executions. No scoring
or classification is triggered by reading this projection.
"""
import hashlib
import json
from collections import defaultdict
from threading import RLock

from sqlalchemy import text
from .database import Conflict

from .catalog import canonical, UNASSIGNED
from .identity import metadata
from .ordering import source_projection
from .run_summary import summarize
from .case_presentation import case_presentation
from .timeline import duration, select_phases
from .extensions.contracts import check

STATES = ('ok', 'error', 'unknown', 'running')
JOB_STATES = ('queued', 'running', 'completed', 'failed', 'cancelled')
FACETS = {'stages': 'base.stage', 'intents': 'base.intent', 'clis': 'base.cli'}


def total_known(values):
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def ratio(item):
    denominator = item['ok'] + item['error']
    return item['error'] / denominator if denominator else None


def tool_timing(records):
    known = [r['ms'] for r in records if r['ms'] is not None]
    return {'tool_ms': sum(known) if known else None, 'tool_time_known': len(known),
            'tool_time_coverage': len(known) / len(records) if records else None}


def compact(trace, scope):
    projected = source_projection(trace)
    phases = select_phases(projected, [p['id'] for p in projected['phases']] if scope == 'all' else None)
    ids = {p['id'] for p in phases}
    spans = [s for s in projected['spans'] if s['phase_id'] in ids]
    header = summarize(projected, phases)
    tools = [{'id': s['id'], 'status': s['status'], 'ms': duration(s)} for s in spans if s['kind'] == 'tool']
    requests = defaultdict(list)
    for s in spans:
        if s['kind'] == 'model':
            requests[(s['agent_id'], s['request_id'], s['attempt'])].append(s.get('usage'))
    usage = []
    for values in requests.values():
        populated = {canonical(v)[0]: v for v in values if v is not None}
        if len(populated) == 1:
            row = next(iter(populated.values()))
            # Standard input_tokens includes cache read/write; thinking is part of output.
            if row['input_tokens'] is not None and row['output_tokens'] is not None:
                usage.append(row['input_tokens'] + row['output_tokens'])
    fragments = None
    if trace['collector']['name'] == 'doubao-command-corpus-adapter':
        fragments = sum(s['id'].startswith('fragment-') for s in trace['sources'])
    return {**metadata(trace)['run'], **header, 'tools': tools, 'record_count': len(spans),
            'source_fragment_count': fragments, 'model_request_count': len(requests) if requests or all(p.get('coverage', trace['coverage'])['model_requests'] == 'complete' for p in phases) else None,
            'token_total': sum(usage) if usage else None, 'token_known_requests': len(usage)}


class CollectionOverview:
    def __init__(self, store, extensions):
        self.store = store
        self.repo = store.repository
        self.extensions = extensions
        self._facts = {}
        self._artifacts = {}
        self._lock = RLock()

    def definitions(self, cid):
        if cid == UNASSIGNED:
            assigned = {r['query_id'] for r in self.repo.rows('SELECT query_id FROM collection_cases')}
            return {'id': cid, 'title': '未分组', 'description': '尚未加入集合的轨迹', 'kind': 'unassigned'}, None, assigned
        rows = self.repo.rows('SELECT payload FROM collections WHERE id=:id', {'id': cid})
        if not rows:
            raise KeyError('集合不存在')
        catalog = json.loads(rows[0]['payload'])
        return catalog['collection'], {c['query_id']: c for c in catalog['cases']}, set()

    def facts(self, scope):
        rows = self.repo.rows('SELECT id,digest,imported_at FROM runs ORDER BY imported_at,id')
        # Cache only compact projections, never the large original arguments/results.
        with self._lock:
            live = {(r['id'], r['digest'], s) for r in rows for s in ('task', 'all')}
            self._facts = {k: v for k, v in self._facts.items() if k in live}
            for r in rows:
                key = (r['id'], r['digest'], scope)
                if key not in self._facts:
                    _, trace = self.store._read(r['id'])
                    self._facts[key] = compact(trace, scope)
            return [{**self._facts[(r['id'], r['digest'], scope)], 'digest': r['digest'],
                     'imported_at': r['imported_at']} for r in rows]

    def selection(self, plugin_id, plugin_version, contribution_id, scope):
        if scope not in ('task', 'all'):
            raise ValueError('scope 必须为 task 或 all')
        if not plugin_id and not plugin_version:
            return None, None
        if not plugin_id or not plugin_version:
            raise ValueError('plugin_id 和 plugin_version 必须同时提供')
        manifest = self.extensions.native(plugin_id, plugin_version)['manifest']
        contribution = next((c for c in manifest['contributes'] if c['id'] == contribution_id), None)
        if not contribution or contribution.get('mode') != 'classify' or contribution.get('output_kind') != 'facets':
            raise ValueError('请选择分类贡献点')
        config_digest = canonical(contribution['default_config'])[1]
        return {'plugin_id': plugin_id, 'plugin_version': plugin_version,
                'contribution_id': contribution_id, 'config_digest': config_digest, 'scope': scope}, contribution

    def classify(self, cid, request):
        allowed = {'plugin_id', 'plugin_version', 'contribution_id', 'scope', 'config', 'request_key'}
        if not isinstance(request, dict) or set(request) - allowed:
            raise ValueError('集合分类请求字段无效')
        if not all(isinstance(request.get(k), str) and request[k] for k in ('plugin_id', 'plugin_version', 'contribution_id', 'request_key')):
            raise ValueError('请提供插件版本、贡献点和请求标识')
        if len(request['request_key']) > 128:
            raise ValueError('request_key 最多 128 字符')
        scope = request.get('scope', 'task')
        selected, contribution = self.selection(request['plugin_id'], request['plugin_version'], request['contribution_id'], scope)
        config = request.get('config', contribution['default_config'])
        check(contribution['config_schema'], config)
        # This read API deliberately aggregates one default configuration. Avoid
        # dispatching custom results which would be invisible to its selector.
        if canonical(config)[1] != selected['config_digest']:
            raise ValueError('集合分类当前使用插件默认配置；自定义配置请用 plugin-runs 接口')
        _, definitions, assigned = self.definitions(cid)
        runs = [r for r in self.facts(scope) if r['query_id'] in definitions] if definitions is not None else [r for r in self.facts(scope) if r['query_id'] not in assigned]
        runs.sort(key=lambda r: r['id'])
        batches = []
        counts = dict.fromkeys(JOB_STATES, 0)
        # Stable chunks make retry safe. Include collection/key, but not the input
        # set: changed inputs must conflict instead of silently creating new work.
        prefix = hashlib.sha256(canonical([cid, request['request_key']])[0].encode()).hexdigest()
        dispatch_payload, dispatch_digest = canonical({'collection_id': cid, 'plugin': selected,
            'runs': [[r['id'], r['digest']] for r in runs]})
        with self.repo.engine.begin() as db:
            db.execute(text('INSERT INTO collection_classifications(request_key,request_digest,payload) VALUES(:key,:digest,:payload) ON CONFLICT(request_key) DO NOTHING'),
                {'key': prefix, 'digest': dispatch_digest, 'payload': dispatch_payload})
            previous = db.execute(text('SELECT request_digest FROM collection_classifications WHERE request_key=:key'), {'key': prefix}).scalar_one()
            if previous != dispatch_digest:
                raise Conflict('该集合请求标识已绑定其他输入或分类参数；新一轮分类请使用新 request_key')
        for i in range(0, len(runs), 50):
            batch = self.extensions.tasks.create({
                'plugin_id': request['plugin_id'], 'plugin_version': request['plugin_version'],
                'contribution_id': request['contribution_id'], 'scope': scope, 'config': config,
                'collection_id': cid, 'run_ids': [r['id'] for r in runs[i:i + 50]],
                'request_key': f'collection-{prefix}-{i // 50}',
            })
            batches.append(batch['id'])
            for state in counts:
                counts[state] += batch['counts'][state]
        return {'collection_id': cid, 'run_count': len(runs), 'batch_ids': batches, 'counts': counts}

    def artifacts(self, cid, selected, runs):
        if selected is None:
            return {}, {}, {}
        handler = self.extensions.tasks.handler_id(selected['plugin_id'], selected['contribution_id'])
        # Match version, config, scope, collection and immutable input digest.
        rows = self.repo.rows('''SELECT j.id,j.run_id,j.input_digest,j.snapshot_digest,j.state,
            j.attempt,a.digest AS artifact_digest FROM plugin_invocations j JOIN plugin_batches b ON b.id=j.batch_id
            LEFT JOIN plugin_artifacts a ON a.job_id=j.id AND a.attempt=j.attempt
            WHERE b.collection_id=:cid AND b.plugin_id=:pid AND b.plugin_version=:v
              AND b.config_digest=:cd AND b.scope=:scope
            ORDER BY b.created_at DESC,b.id DESC,j.position''',
            {'cid': cid, 'pid': handler, 'v': selected['plugin_version'], 'cd': selected['config_digest'], 'scope': selected['scope']})
        by_run = {r['id']: r for r in runs}
        states, outputs, labels = {}, {}, {}
        for row in rows:
            run = by_run.get(row['run_id'])
            if run is None or row['input_digest'] != run['digest']:
                continue
            states.setdefault(row['run_id'], row['state'])
            if row['run_id'] in outputs or not row['artifact_digest'] or row['state'] != 'completed':
                continue
            key = (row['id'], row['attempt'], row['artifact_digest'])
            with self._lock:
                cached = self._artifacts.get(key)
                if cached is None:
                    artifact = json.loads(self.repo.rows('SELECT payload FROM plugin_artifacts WHERE job_id=:id AND attempt=:attempt',
                        {'id': row['id'], 'attempt': row['attempt']})[0]['payload'])
                    output = artifact['output']
                    if output['kind'] != 'facets':
                        continue
                    data = output['data']
                    names = {f['id']: {v['id']: v['title'] for v in f['values']} for f in data['facets']}
                    assignments = defaultdict(dict)
                    for item in data['assignments']:
                        target = item['target']
                        if target['document_id'] != run['id'] or target['document_digest'] != run['digest'] or target['entity_kind'] != 'span':
                            continue
                        assignments[target['entity_id']][item['facet_id']] = item['value_ids'] if item['status'] == 'assigned' and item['value_ids'] else ['__unknown__']
                    cached = self._artifacts[key] = (artifact['input_ref'], assignments, names)
            input_ref, assignments, names = cached
            if input_ref['selection_digest'] != row['snapshot_digest'] or input_ref['document_digest'] != run['digest']:
                continue
            for facet_id, values in names.items():
                labels.setdefault(facet_id, {}).update(values)
            outputs[run['id']] = assignments
        return outputs, states, labels

    def overview(self, cid, *, plugin_id=None, plugin_version=None, contribution_id='classify', scope='task',
                 stage=None, intent=None, cli=None, status=None, conversation=None, fragments=None, query_id=None, q='', page=1, page_size=25, sort='errors_desc'):
        if sort not in ('errors_desc', 'recent'):
            raise ValueError('sort 必须为 errors_desc 或 recent')
        if not 1 <= page or not 1 <= page_size <= 100:
            raise ValueError('page 必须大于 0，page_size 必须为 1–100')
        for value, allowed, name in [(status, STATES, 'status'), (conversation, ('single_turn', 'multi_turn', 'unknown'), 'conversation'),
                                      (fragments, ('single', 'multiple', 'unknown'), 'fragments')]:
            if value and value not in allowed:
                raise ValueError(name + ' 筛选值无效')
        collection, definitions, assigned = self.definitions(cid)
        runs = [r for r in self.facts(scope) if r['query_id'] in definitions] if definitions is not None else [r for r in self.facts(scope) if r['query_id'] not in assigned]
        definitions = definitions if definitions is not None else {r['query_id']: {'query_id': r['query_id'], 'title': r['title'], 'conversation': {'mode': 'unknown'}} for r in runs}
        selected, _ = self.selection(plugin_id, plugin_version, contribution_id, scope)
        outputs, states, labels = self.artifacts(cid, selected, runs)
        progress = {'total': len(runs), 'unclassified': sum(r['id'] not in states for r in runs),
                    **{state: sum(states.get(r['id']) == state for r in runs) for state in JOB_STATES}}
        by_query, all_records = defaultdict(list), []
        for run in runs:
            by_query[run['query_id']].append(run)
            for tool in run['tools']:
                values = outputs.get(run['id'], {}).get(tool['id'], {})
                all_records.append({**tool, 'run_id': run['id'], 'query_id': run['query_id'],
                    **{key: values.get(facet, ['__unknown__' if run['id'] in outputs else '__unclassified__']) for key, facet in FACETS.items()}})
        def metric(selected_runs, records, case_count):
            turns = [r['user_turn_count'] for r in selected_runs]
            result = {'case_count': case_count, 'run_count': len(selected_runs),
                'error_case_count': len({r['query_id'] for r in records if r['status'] == 'error'}),
                'record_count': sum(r['record_count'] for r in selected_runs), 'tool_count': len(records),
                **{s: sum(t['status'] == s for t in records) for s in STATES},
                'classified_run_count': sum(r['id'] in outputs for r in selected_runs),
                'wall_ms': total_known(r['wall_ms'] for r in selected_runs), 'wall_known_runs': sum(r['wall_ms'] is not None for r in selected_runs),
                'user_turn_count': total_known(turns), 'user_turn_known_runs': sum(t is not None for t in turns),
                'model_request_count': total_known(r['model_request_count'] for r in selected_runs),
                'token_total': total_known(r['token_total'] for r in selected_runs), 'token_known_requests': sum(r['token_known_requests'] for r in selected_runs),
                'source_fragment_count': total_known(r['source_fragment_count'] for r in selected_runs),
                'multi_fragment_run_count': sum((r['source_fragment_count'] or 0) > 1 for r in selected_runs),
                **tool_timing(records)}
            return {**result, 'error_rate': ratio(result)}
        records_by_query = defaultdict(list)
        for record in all_records:
            records_by_query[record['query_id']].append(record)
        cases, matching_records, matching_runs = [], [], []
        for ordinal, (case_query_id, definition) in enumerate(definitions.items(), start=1):
            if query_id and query_id != case_query_id:
                continue
            case_runs = by_query[case_query_id]
            records = records_by_query[case_query_id]
            presentation = case_presentation(definition, case_runs, ordinal)
            mode = definition['conversation']['mode']
            # Conversation mode is the declared Case shape; observed rounds stay separate.
            if conversation and conversation != mode:
                continue
            if q and q.casefold() not in ' '.join([case_query_id, definition['title'], *presentation.values(), *(r['query'] for r in case_runs)]).casefold():
                continue
            if fragments and not any(('unknown' if r['source_fragment_count'] is None else 'multiple' if r['source_fragment_count'] > 1 else 'single') == fragments for r in case_runs):
                continue
            hits = [r for r in records if (not stage or stage in r['stages']) and (not intent or intent in r['intents']) and (not cli or (len(r['clis']) > 1 if cli == '__multiple__' else cli in r['clis'])) and (not status or status == r['status'])]
            if (stage or intent or cli or status) and not hits:
                continue
            # Case row metrics describe its whole trace(s); matched count exposes
            # the precise drill-down predicate without losing context.
            stats = metric(case_runs, records, 1)
            known = stats['classified_run_count']
            coverage = [r['user_turn_coverage'] for r in case_runs]
            cases.append({'query_id': case_query_id, 'title': definition['title'], **presentation, 'conversation_mode': mode,
                **stats, 'matched_tool_count': len(hits),
                'stages': sorted({v for r in records for v in r['stages']}),
                'intents': sorted({v for r in records for v in r['intents']}),
                'harnesses': sorted({r['harness'] for r in case_runs}), 'models': sorted({r['model'] for r in case_runs}),
                'classification_state': 'completed' if known and known == len(case_runs) else 'partial' if known else 'unclassified',
                'user_turn_coverage': 'complete' if coverage and all(c == 'complete' for c in coverage) else 'partial' if any(c != 'missing' for c in coverage) else 'missing',
                'latest_imported_at': max((r['imported_at'] for r in case_runs), default='')})
            matching_records.extend(records)
            matching_runs.extend(case_runs)
        def breakdown(selected_records):
            result = {}
            recorded_ms = total_known(r['ms'] for r in selected_records)
            for key, facet in FACETS.items():
                groups = defaultdict(list)
                for r in selected_records:
                    for value in (['__multiple__'] if key == 'clis' and len(r[key]) > 1 else r[key]):
                        groups[value].append(r)
                items = []
                for value, records in groups.items():
                    timing = tool_timing(records)
                    item = {'value': value, 'label': {'__unknown__': '未识别', '__unclassified__': '未分类', '__multiple__': '多 CLI / 共享调用'}.get(value, labels.get(facet, {}).get(value, value)),
                            'tool_count': len(records), **{state: sum(r['status'] == state for r in records) for state in STATES},
                            'case_count': len({r['query_id'] for r in records}), 'run_count': len({r['run_id'] for r in records}),
                            **timing, 'tool_time_share': timing['tool_ms'] / recorded_ms if timing['tool_ms'] is not None and recorded_ms else None}
                    items.append({**item, 'error_rate': ratio(item)})
                result[key] = sorted(items, key=lambda r: (-r['error'], -r['tool_count'], r['value']))
            return result
        # Stable successive sorts apply the most significant criterion last.
        # Error count is a recorded fact, never an inferred quality score.
        cases.sort(key=lambda c: c['query_id'])
        cases.sort(key=lambda c: c['latest_imported_at'], reverse=True)
        if sort == 'errors_desc':
            cases.sort(key=lambda c: c['error'], reverse=True)
        total = len(cases)
        return {'collection': collection, 'plugin': selected, 'summary': metric(runs, all_records, len(definitions)),
                'filtered_summary': metric(matching_runs, matching_records, total), 'progress': progress,
                'facets': breakdown(all_records), 'filtered_facets': breakdown(matching_records), 'cases': cases[(page - 1) * page_size:page * page_size],
                'pagination': {'sort': sort, 'page': page, 'page_size': page_size, 'total': total, 'total_pages': (total + page_size - 1) // page_size},
                'notes': ['错误率 = 失败 / (成功 + 失败)；数量按记录统计，不代表独立执行次数。',
                          '错误优先按记录到的失败数排序，不是质量评分。',
                          '筛选汇总与 filtered_facets 包含命中 Case 的完整记录。',
                          '工具时间占比仅以已记录工具时长为分母；缺失保持未知，不代表墙钟时间。',
                          '来源片段不等于用户轮次；CLI 失败记录不证明 CLI 本身故障。']}
