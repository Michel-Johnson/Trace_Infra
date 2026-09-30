"""Offline checks for the general plugin proposal, never a runtime registry."""
import json
from pathlib import Path
from jsonschema import Draft202012Validator
from .plugin_validation import unique,check_local_refs,entity_key

DIRECTORY = Path(__file__).resolve().parents[2] / 'contracts/drafts/plugin-v2'


def schema(name):
    return json.loads((DIRECTORY / (name + '.schema.json')).read_text())






def validate_manifest(value):
    json.dumps(value, allow_nan=False)
    Draft202012Validator(schema('plugin')).validate(value)
    unique([c['id'] for c in value['contributes']], '贡献点 ID 重复')
    for item in value['contributes']:
        check_local_refs(item['config_schema'])
        Draft202012Validator.check_schema(item['config_schema'])
        Draft202012Validator(item['config_schema']).validate(item['default_config'])
        if item['kind'] == 'evaluator':
            unique([m['key'] for m in item['metrics']], '指标 key 重复')
    return value




def validate_output(value, allowed_entities=None):
    """Optionally compare against an explicit synthetic input scope in tests.

    Production authorization and input resolution are not implemented here.
    """
    kind = {'trace-hunter/facets/1.0-draft.1': 'facets',
            'trace-hunter/selection/1.0-draft.1': 'selection'}.get(value.get('schema_version'))
    if kind is None:
        raise ValueError('不是支持的输出合同')
    json.dumps(value, allow_nan=False)
    Draft202012Validator(schema(kind)).validate(value)
    sources = [(r['document_id'], r['document_digest']) for r in value['input_refs']]
    unique(sources, '同一输入文档重复')
    refs = value['members'] if kind == 'selection' else [r for a in value['assignments'] for r in [a['target'], *a['evidence_refs']]]
    for ref in refs:
        if (ref['document_id'], ref['document_digest']) not in sources:
            raise ValueError('实体不属于固定输入文档')
        if allowed_entities is not None and entity_key(ref) not in allowed_entities:
            raise ValueError('实体不属于给定的输入范围')
    if kind == 'selection':
        unique([entity_key(ref) for ref in value['members']], '选择成员重复')
        return value
    unique([f['id'] for f in value['facets']], '分类维度 ID 重复')
    definitions = {f['id']: f for f in value['facets']}
    for facet in value['facets']:
        unique([v['id'] for v in facet['values']], '分类值 ID 重复')
    assignments = []
    for assignment in value['assignments']:
        facet = definitions.get(assignment['facet_id'])
        if facet is None or not set(assignment['value_ids']).issubset({v['id'] for v in facet['values']}):
            raise ValueError('未声明的分类维度或分类值')
        if facet['cardinality'] == 'single' and len(assignment['value_ids']) > 1:
            raise ValueError('单选分类不能分配多个值')
        assignments.append((assignment['facet_id'], entity_key(assignment['target'])))
    unique(assignments, '同一实体的分类分配重复')
    if value['coverage'] == 'complete' and allowed_entities is not None:
        expected = {(facet, target) for facet in definitions for target in allowed_entities}
        if set(assignments) != expected:
            raise ValueError('完整覆盖必须处理范围内全部实体和分类维度')
    return value
