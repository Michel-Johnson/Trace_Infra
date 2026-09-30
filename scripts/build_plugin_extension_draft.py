"""Offline general plugin proposal. Does not change the deployed evaluator API."""
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'trace-hunter/plugin/2.0-draft.1'
FACETS = 'trace-hunter/facets/1.0-draft.1'
SELECTION = 'trace-hunter/selection/1.0-draft.1'


def obj(properties, required=None):
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required,
            'additionalProperties': False}


def enum(*values):
    return {'enum': list(values)}


def array(items, minimum=0, maximum=100):
    return {'type': 'array', 'items': items, 'minItems': minimum, 'maxItems': maximum}


ID = {'type': 'string', 'minLength': 1, 'maxLength': 256}
TEXT = {'type': 'string', 'maxLength': 10000}
HASH = {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}
REF = obj({'document_id': ID, 'document_digest': HASH,
           'entity_kind': enum('collection', 'case', 'run', 'turn', 'span', 'artifact'), 'entity_id': ID})
INPUT_REF = obj({'document_id': ID, 'document_digest': HASH, 'selection_digest': HASH})


def build_manifest():
    metric = json.loads((ROOT / 'contracts/schemas/plugin-v1.schema.json').read_text())['properties']['metrics']

    def contribution(kind, hosts, trigger, output, extra=None, permissions=()):
        return obj({
            'id': ID, 'title': ID, 'kind': {'const': kind},
            'implementation': obj({'host': enum(*hosts), 'ref': ID}),
            'scopes': {**array(enum('collection', 'case', 'run', 'turn', 'span', 'artifact', 'comparison'), 1), 'uniqueItems': True},
            'consumes': {**array(ID, 1), 'uniqueItems': True},
            'config_schema': {'type': 'object'}, 'default_config': {'type': 'object'},
            'trigger': {'const': trigger}, 'output_kind': {'const': output},
            'permissions': {**array(enum(*permissions)), 'uniqueItems': True},
            **(extra or {}),
        })

    read = ('trace.read', 'derived.read')
    renderer = contribution('renderer', ['browser'], 'view', 'view',
        {'mounts': {**array(enum('collection.case_table', 'case.comparison', 'run.timeline', 'record.detail', 'result.detail'), 1), 'uniqueItems': True}}, read)
    evaluator = contribution('evaluator', ['server', 'remote_agent'], 'explicit', 'evaluation',
        {'metrics': metric, 'requirements': json.loads((ROOT / 'contracts/schemas/plugin-v1.schema.json').read_text())['properties']['requirements']}, (*read, 'evaluation.submit'))
    filtering = contribution('slicer', ['browser', 'server'], 'interaction', 'selection',
        {'mode': {'const': 'filter'}}, read)
    classify = contribution('slicer', ['server', 'remote_agent'], 'explicit', 'facets',
        {'mode': {'const': 'classify'}}, (*read, 'facets.submit'))
    schema = obj({
        'schema_version': {'const': VERSION},
        'plugin_id': {'type': 'string', 'pattern': '^[a-z][a-z0-9.-]{2,95}$'},
        'version': {'type': 'string', 'pattern': r'^[0-9]+\.[0-9]+\.[0-9]+$'},
        'title': ID, 'description': TEXT, 'package_digest': HASH,
        'contributes': array({'oneOf': [renderer, evaluator, filtering, classify]}, 1),
        'extensions': {'type': 'object', 'patternProperties': {r'^[a-z][a-z0-9_-]*\.[a-zA-Z0-9_.-]+$': {}}, 'additionalProperties': False},
    })
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema',
            '$id': 'urn:trace-hunter:plugin:2.0-draft.1', **schema}


def build_facets():
    assignment = obj({
        'target': REF, 'facet_id': ID, 'status': enum('assigned', 'unknown', 'not_applicable'),
        'value_ids': {**array(ID), 'uniqueItems': True},
        'reason': {'type': ['string', 'null'], 'maxLength': 10000},
        'evidence_refs': array(REF),
    })
    assignment['allOf'] = [
        {'if': {'properties': {'status': {'const': 'assigned'}}},
         'then': {'properties': {'value_ids': {'minItems': 1}}}},
        {'if': {'properties': {'status': enum('unknown', 'not_applicable')}},
         'then': {'properties': {'value_ids': {'maxItems': 0}, 'reason': ID}}},
    ]
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema', **obj({
        'schema_version': {'const': FACETS},
        'input_refs': array(INPUT_REF, 1),
        'coverage': enum('complete', 'partial', 'unknown'),
        'facets': array(obj({'id': ID, 'title': ID, 'cardinality': enum('single', 'multiple'),
                             'values': array(obj({'id': ID, 'title': ID}), 1)}), 1),
        'assignments': array(assignment, maximum=20000),
    })}


def build_selection():
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema', **obj({
        'schema_version': {'const': SELECTION}, 'input_refs': array(INPUT_REF, 1),
        'members': array(REF, maximum=20000),
    })}


def wrap_evaluator_v1(manifest):
    """Offline compatibility projection. Original manifest and results stay immutable."""
    return {
        'schema_version': VERSION, 'plugin_id': manifest['plugin_id'], 'version': manifest['version'],
        'title': manifest['title'], 'description': manifest['description'],
        'package_digest': manifest['package_digest'], 'extensions': {},
        'contributes': [{
            'id': 'evaluate', 'title': manifest['title'], 'kind': 'evaluator',
            'implementation': {'host': 'remote_agent' if manifest['entrypoint']['kind'] == 'agent_skill' else 'server', 'ref': manifest['entrypoint']['ref']},
            'scopes': copy.deepcopy(manifest['scopes']), 'consumes': copy.deepcopy(manifest['trace_versions']),
            'config_schema': copy.deepcopy(manifest['config_schema']), 'default_config': copy.deepcopy(manifest['default_config']),
            'trigger': 'explicit', 'output_kind': 'evaluation', 'permissions': ['trace.read', 'evaluation.submit'],
            'metrics': copy.deepcopy(manifest['metrics']), 'requirements': copy.deepcopy(manifest['requirements']),
        }],
    }


def examples():
    def capability(cid, kind, host, trigger, output, permissions, **extra):
        return {'id': cid, 'title': cid, 'kind': kind, 'implementation': {'host': host, 'ref': 'example/' + cid},
                'scopes': ['run'], 'consumes': ['trace-hunter/1.1', FACETS],
                'config_schema': obj({}), 'default_config': {},
                'trigger': trigger, 'output_kind': output, 'permissions': permissions, **extra}
    classifier = capability('classify-stage', 'slicer', 'remote_agent', 'explicit', 'facets', ['trace.read', 'facets.submit'], mode='classify')
    classifier['consumes'] = ['trace-hunter/1.1']
    renderer = capability('stage-timeline', 'renderer', 'browser', 'view', 'view', ['trace.read', 'derived.read'], mounts=['run.timeline'])
    slicer = capability('filter-stage', 'slicer', 'browser', 'interaction', 'selection', ['trace.read', 'derived.read'], mode='filter')
    def manifest(name, contributions):
        return {'schema_version': VERSION, 'plugin_id': 'example.' + name, 'version': '1.0.0', 'title': name,
                'description': '合成协议样例；零摘要为占位，不代表已发布的插件包。',
                'package_digest': '0' * 64, 'contributes': contributions, 'extensions': {'example.synthetic': True}}
    source = {'document_id': 'trace-demo', 'document_digest': '0' * 64, 'selection_digest': '0' * 64}
    target = {'document_id': 'trace-demo', 'document_digest': '0' * 64, 'entity_kind': 'span', 'entity_id': 'tool-1'}
    return {
        'renderer-only': manifest('timeline', [renderer]),
        'stage-classifier-suite': manifest('stage-suite', [classifier, slicer, renderer]),
        'evaluator-compatibility': wrap_evaluator_v1(json.loads((ROOT / 'plugins/official/time/manifest.json').read_text())),
        'facets-partial': {'schema_version': FACETS, 'input_refs': [source], 'coverage': 'partial',
            'facets': [{'id': 'stage', 'title': '执行阶段', 'cardinality': 'single', 'values': [{'id': 'explore', 'title': '探索'}, {'id': 'implement', 'title': '实现'}]}],
            'assignments': [{'target': target, 'facet_id': 'stage', 'status': 'assigned', 'value_ids': ['explore'], 'reason': '合成示例：读取项目说明。', 'evidence_refs': [target]},
                            {'target': {**target, 'entity_id': 'tool-2'}, 'facet_id': 'stage', 'status': 'unknown', 'value_ids': [], 'reason': '记录缺少分类所需的内容。', 'evidence_refs': []}]},
        'selection-empty': {'schema_version': SELECTION, 'input_refs': [source], 'members': []},
    }


def main():
    path = ROOT / 'contracts/drafts/plugin-v2';path.mkdir(parents=True, exist_ok=True)
    for name, schema in [('plugin', build_manifest()), ('facets', build_facets()), ('selection', build_selection())]:
        (path / (name + '.schema.json')).write_text(json.dumps(schema, ensure_ascii=False, indent=2) + '\n')
    target = ROOT / 'examples/drafts/plugin-v2';target.mkdir(parents=True, exist_ok=True)
    for name, value in examples().items():
        (target / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
