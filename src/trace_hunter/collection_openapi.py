"""Compact, paginated collection projection and explicit batch classification."""
from .evaluations.contracts import obj, enum, ID, COUNT, DIGEST


def extend(schemas, paths):
    ref = lambda name: {'$ref': '#/components/schemas/' + name}
    array = lambda item: {'type': 'array', 'items': item}
    optional_count = {'type': ['integer', 'null'], 'minimum': 0}
    states = {state: COUNT for state in ('queued', 'running', 'completed', 'failed', 'cancelled')}
    statuses = {state: COUNT for state in ('ok', 'error', 'unknown', 'running')}
    schemas['CollectionPluginSelection'] = obj({'plugin_id': ID, 'plugin_version': ID, 'contribution_id': ID,
        'config_digest': DIGEST, 'scope': enum('task', 'all')})
    metrics = {k: COUNT for k in ('case_count', 'error_case_count', 'run_count', 'record_count', 'tool_count', 'classified_run_count',
        'wall_known_runs', 'user_turn_known_runs', 'token_known_requests', 'multi_fragment_run_count', 'tool_time_known')}
    metrics.update({k: optional_count for k in ('user_turn_count', 'model_request_count', 'token_total', 'source_fragment_count')})
    metrics.update(statuses)
    metrics.update(wall_ms={'type': ['number', 'null'], 'minimum': 0}, error_rate={'type': ['number', 'null'], 'minimum': 0, 'maximum': 1})
    metrics.update(tool_ms={'type': ['number', 'null'], 'minimum': 0, 'description': '已记录工具时长累计；缺失不归零；不是墙钟时间。'}, tool_time_coverage=metrics['error_rate'])
    schemas['CollectionMetrics'] = obj(metrics)
    schemas['CollectionBreakdown'] = obj({'value': ID, 'label': ID, **{k: COUNT for k in ('tool_count', 'case_count', 'run_count')},
        **statuses, 'error_rate': metrics['error_rate'],
        'tool_ms': metrics['tool_ms'], 'tool_time_known': COUNT, 'tool_time_coverage': metrics['tool_time_coverage'],
        'tool_time_share': {**metrics['error_rate'], 'description': '本分类已记录工具累计时长 / 当前集合或筛选Case的已记录工具累计时长；未知或分母0为null。'}})
    schemas['CollectionCaseRow'] = obj({**metrics, 'query_id': ID, 'title': {'type': 'string'},
        'display_id': {'type': 'string', 'pattern': '^category [0-9]{3,}$', 'description': '集合清单原始顺序对应的展示编号，不受筛选或排序影响；不是 query_id。'},
        'display_description': {'type': 'string', 'maxLength': 240, 'description': '优先 Case description，否则首个输入的第一句话；只读展示，原始内容保留。'},
        'conversation_mode': enum('single_turn', 'multi_turn', 'unknown'), 'matched_tool_count': COUNT,
        **{k: array({'type': 'string'}) for k in ('stages', 'intents', 'harnesses', 'models')},
        'classification_state': enum('unclassified', 'partial', 'completed'),
        'user_turn_coverage': enum('complete', 'partial', 'missing'), 'latest_imported_at': {'type': 'string'}})
    schemas['CollectionOverview'] = obj({
        'collection': obj({k: {'type': 'string'} for k in ('id', 'title', 'description', 'kind')}),
        'plugin': {'anyOf': [ref('CollectionPluginSelection'), {'type': 'null'}]},
        'summary': ref('CollectionMetrics'), 'filtered_summary': ref('CollectionMetrics'),
        'progress': obj({'total': COUNT, 'unclassified': COUNT, **states}),
        'facets': obj({k: array(ref('CollectionBreakdown')) for k in ('stages', 'intents', 'clis')}),
        'filtered_facets': obj({k: array(ref('CollectionBreakdown')) for k in ('stages', 'intents', 'clis')}),
        'cases': array(ref('CollectionCaseRow')),
        'pagination': obj({**{k: COUNT for k in ('page', 'page_size', 'total', 'total_pages')}, 'sort': enum('errors_desc', 'recent')}),
        'notes': array({'type': 'string'}),
    })
    schemas['ClassifyCollectionRequest'] = obj({'plugin_id': ID, 'plugin_version': ID, 'contribution_id': ID,
        'scope': enum('task', 'all'), 'config': {'type': 'object'},
        'request_key': {'type': 'string', 'minLength': 1, 'maxLength': 128}},
        ['plugin_id', 'plugin_version', 'contribution_id', 'request_key'])
    schemas['ClassifyCollectionResult'] = obj({'collection_id': ID, 'run_count': COUNT, 'batch_ids': array(ID), 'counts': obj(states)})
    path_parameter = {'name': 'collection_id', 'in': 'path', 'required': True, 'schema': ID}
    parameters = [path_parameter]
    options = {k: {'type': 'string'} for k in ('plugin_id', 'plugin_version', 'contribution_id', 'stage', 'intent', 'cli', 'query_id', 'q')}
    options.update(sort={**enum('errors_desc', 'recent'), 'default': 'errors_desc'}, scope={**enum('task', 'all'), 'default': 'task'}, status=enum('ok', 'error', 'unknown', 'running'),
        conversation=enum('single_turn', 'multi_turn', 'unknown'), fragments=enum('single', 'multiple', 'unknown'),
        page={'type': 'integer', 'minimum': 1, 'default': 1}, page_size={'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 25})
    parameters.extend({'name': k, 'in': 'query', 'required': False, 'schema': v} for k, v in options.items())
    def responses(name):
        return {'200': {'description': '成功', 'content': {'application/json': {'schema': ref(name)}}},
            **{str(s): {'$ref': '#/components/responses/Error' + str(s)} for s in (403, 404, 409, 413, 415, 422, 500)}}
    paths['/api/collections/{collection_id}/overview'] = {'get': {
        'operationId': 'getCollectionOverview', 'tags': ['Collections'],
        'summary': '分页 Case 与已记录事实及选定插件产物汇总；不返回轨迹正文，不创建分析任务',
        'parameters': parameters, 'responses': responses('CollectionOverview'), 'x-triggers-analysis': False}}
    paths['/api/collections/{collection_id}/classify'] = {'post': {
        'operationId': 'classifyCollection', 'tags': ['Collections'],
        'summary': '显式分类整个集合，按最多 50 个运行分块并通过 request_key 保持幂等',
        'parameters': [path_parameter], 'responses': responses('ClassifyCollectionResult'), 'x-triggers-analysis': True,
        'requestBody': {'required': True, 'content': {'application/json': {'schema': ref('ClassifyCollectionRequest')}}}}}
