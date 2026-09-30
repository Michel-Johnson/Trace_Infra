"""Stable extension schemas and semantic validation."""
import json
from pathlib import Path
from ..evaluations.contracts import SCORE,CREATE as EVAL_CREATE,obj,enum,ID,DIGEST,COUNT,check
from ..plugin_validation import check_local_refs,entity_key,unique
from jsonschema import Draft202012Validator

ROOT=Path(__file__).resolve().parents[3]
MANIFEST=json.loads((ROOT/'contracts/schemas/plugin-v2.schema.json').read_text())
FACETS=json.loads((ROOT/'contracts/schemas/facets-v1.schema.json').read_text())
SELECTION=json.loads((ROOT/'contracts/schemas/selection-v1.schema.json').read_text())
CREATE=obj({**EVAL_CREATE['properties'],'contribution_id':ID})
OUTPUT={'type':'object','oneOf':[obj({'kind':{'const':'facets'},'data':FACETS,'usage':SCORE['properties']['usage']}),obj({'kind':{'const':'evaluation'},'data':SCORE})]}

def validate_manifest(value):
    check(MANIFEST,value)
    unique([c['id'] for c in value['contributes']],'贡献点 ID 重复')
    targets=value['extensions'].get('trace_hunter.classification_targets',{})
    check({'type':'object','additionalProperties':{'type':'array','items':enum('tool','model','wait','agent'),'minItems':1,'uniqueItems':True}},targets)
    classifiers={c['id'] for c in value['contributes'] if c.get('mode')=='classify' and c['trigger']=='explicit'}
    if not set(targets).issubset(classifiers):raise ValueError('分类目标只能声明在 classify 贡献点上')
    for contribution in value['contributes']:
        check_local_refs(contribution['config_schema'])
        try:Draft202012Validator.check_schema(contribution['config_schema'])
        except Exception:raise ValueError('配置 Schema 无效') from None
        check(contribution['config_schema'],contribution['default_config'])
        if contribution['kind']=='evaluator':unique([m['key'] for m in contribution['metrics']],'指标重复')
    return value

def classification_targets(definition,snapshot):
    kinds=definition.get('extensions',{}).get('trace_hunter.classification_targets',{}).get(definition['contribution_id'],['tool'])
    return [s for s in snapshot['spans'] if s['kind'] in kinds]

def validate_facets(value,expected_ref,allowed):
    check(FACETS,value)
    if value['input_refs']!=[expected_ref]:raise ValueError('分类结果没有绑定本次固定输入范围')
    definitions={f['id']:f for f in value['facets']}
    unique([f['id'] for f in value['facets']],'分类维度重复')
    for facet in value['facets']:unique([v['id'] for v in facet['values']],'分类值重复')
    assigned=[]
    for item in value['assignments']:
        facet=definitions.get(item['facet_id'])
        if facet is None or not set(item['value_ids']).issubset({v['id'] for v in facet['values']}):raise ValueError('未知分类维度或值')
        if facet['cardinality']=='single' and len(item['value_ids'])>1:raise ValueError('单选分类不能分配多个值')
        for ref in [item['target'],*item['evidence_refs']]:
            if entity_key(ref) not in allowed:raise ValueError('分类引用超出本次声明的输入范围')
        assigned.append((item['facet_id'],entity_key(item['target'])))
    unique(assigned,'分类分配重复')
    if value['coverage']=='complete' and set(assigned)!={(facet,target) for facet in definitions for target in allowed}:raise ValueError('分类完整覆盖声明与实际成员不一致')

def project_evaluator(manifest):
    return {'schema_version':'trace-hunter/plugin/2.0','plugin_id':manifest['plugin_id'],'version':manifest['version'],
        'title':manifest['title'],'description':manifest['description'],'package_digest':manifest['package_digest'],'extensions':{},
        'contributes':[{'id':'evaluate','title':manifest['title'],'kind':'evaluator',
            'implementation':{'host':'remote_agent' if manifest['entrypoint']['kind']=='agent_skill' else 'server','ref':manifest['entrypoint']['ref']},
            'scopes':manifest['scopes'],'consumes':manifest['trace_versions'],'config_schema':manifest['config_schema'],'default_config':manifest['default_config'],
            'trigger':'explicit','output_kind':'evaluation','permissions':['trace.read','evaluation.submit'],'metrics':manifest['metrics'],'requirements':manifest['requirements']}]}
