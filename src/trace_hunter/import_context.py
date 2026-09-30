"""Explicit, user-supplied case and environment identities for a source import."""
import copy
import re
from pathlib import Path

from .catalog import validate_catalog
from .corpus_import import canonical, decode, digest, epoch
from .identity import unknown_environment
from .protocol import SCHEMA
from jsonschema import Draft202012Validator

CONTEXT_VERSION='trace-hunter/doubao-import-context/1.0'
ENV_VALIDATOR=Draft202012Validator(SCHEMA['properties']['environment'])


def load_context(path, conversation_ids):
    if path is None:return None
    raw=Path(path).read_bytes();value=decode(raw)
    if not isinstance(value,dict) or set(value)-{'schema_version','cases','environments','sessions'}:
        raise ValueError('Import context fields must be schema_version, cases, environments and sessions')
    if value.get('schema_version')!=CONTEXT_VERSION:
        raise ValueError('Unsupported import context schema_version')
    cases=value.get('cases',[]);environments=value.get('environments',{});sessions=value.get('sessions',{})
    catalog={'schema_version':'trace-hunter/catalog/1.0','collection':{'id':'import-context','title':'Import context','kind':'task','description':''},'cases':cases}
    validate_catalog(catalog)
    if not isinstance(environments,dict) or not isinstance(sessions,dict):
        raise ValueError('Context environments and sessions must be objects keyed by ID')
    case_map={case['query_id']:case for case in cases}
    env_map={}
    for eid,environment in environments.items():
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}',eid) or not isinstance(environment,dict):
            raise ValueError('Invalid environment identity or definition')
        env={**unknown_environment(),**environment}
        errors=list(ENV_VALIDATOR.iter_errors(env))
        if errors:raise ValueError('Invalid environment definition: '+eid+' /'+ '/'.join(map(str,errors[0].absolute_path)))
        if env['observed_at'] is not None:epoch(env['observed_at'])
        env_map[eid]=env
    for cid,entry in sessions.items():
        if cid not in conversation_ids:raise ValueError('Import context contains an unknown conversation ID')
        if not isinstance(entry,dict) or not entry or set(entry)-{'query_id','env_id','model'}:
            raise ValueError('A context session may specify query_id, env_id and model only')
        if 'query_id' in entry and (not isinstance(entry['query_id'], str) or entry['query_id'] not in case_map):
            raise ValueError('Context query_id must reference a complete Case definition')
        if 'env_id' in entry and (not isinstance(entry['env_id'], str) or entry['env_id'] not in env_map):
            raise ValueError('Context env_id must reference a declared environment')
        if 'model' in entry and (not isinstance(entry['model'],str) or not 0<len(entry['model'])<=4096):
            raise ValueError('Context model must be a nonempty string, not inferred from a filename')
    if not sessions: raise ValueError('Import context must map at least one source conversation')
    return {'raw':raw,'sha256':digest(raw),'cases':case_map,'environments':env_map,'sessions':sessions}


def apply_context(trace, audit, context):
    """Add identities as declared metadata, never as source-observed model events."""
    if not context or audit['conversation_id'] not in context['sessions']:return None
    cid=audit['conversation_id'];entry=context['sessions'][cid];case=None
    previews=[]
    trace['run']['id']+='-ctx-'+context['sha256'][:12]
    audit['run_id']=trace['run']['id']
    if 'query_id' in entry:
        case=copy.deepcopy(context['cases'][entry['query_id']])
        trace['run']['query_id']=entry['query_id'];audit['query_id']=entry['query_id']
        trace['run']['title']=case['title']
        # The catalog can hold full expected prompts. The run header is a preview;
        # these are not proof that all declared turns were observed in the export.
        prompts=case['conversation']['turns']
        if prompts:
            text=prompts[0]['prompt']
            trace['run']['query']=text if len(text)<=4096 else text[:4095]+'…'
            if len(text)>4096:
                previews.append({'target':'run.query','source_length':len(text),'limit':4096,
                                 'complete_prompt':'import-context.cases[query_id].conversation.turns[0].prompt'})
    if 'env_id' in entry:
        trace['run']['env_id']=entry['env_id'];audit['env_id']=entry['env_id']
        trace['environment']=copy.deepcopy(context['environments'][entry['env_id']])
    if 'model' in entry:trace['run']['model']=entry['model']
    trace['sources'].append({'id':'import-context','name':'raw/import-context.json','sha256':context['sha256']})
    audit['import_context']={'sha256':context['sha256'],'fields':sorted(entry),'provenance':'user_declared','header_previews':previews}
    pointer = '/sessions/' + cid.replace('~', '~0').replace('/', '~1')
    trace['evidence'].append({'id': 'declared-import-context', 'kind': 'note',
        'name': '用户提供的题目、环境和模型映射；不代表来源观测到模型请求或完整轮次',
        'status': 'observed', 'detail': canonical({**audit['import_context'], 'declaration': entry}),
        'span_ids': [], 'source': {'source_id': 'import-context', 'pointer': pointer}})
    return case
