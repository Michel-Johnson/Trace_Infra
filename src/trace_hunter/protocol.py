"""Structural schema + cross-record integrity checks at the import boundary."""
import json, math, re
from datetime import datetime
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker
ROOT=Path(__file__).resolve().parents[2]
SCHEMA=json.loads((ROOT/'schemas/trace-v1.schema.json').read_text())
VALIDATOR=Draft202012Validator(SCHEMA, format_checker=FormatChecker())
LEGACY_SCHEMA=json.loads((ROOT/'schemas/trace-v1.0.schema.json').read_text())
LEGACY_VALIDATOR=Draft202012Validator(LEGACY_SCHEMA)
class InvalidTrace(ValueError):
    def __init__(self, errors):
        self.errors=errors[:30]
        super().__init__('; '.join(self.errors))
def validate(trace):
    validator=LEGACY_VALIDATOR if isinstance(trace,dict) and trace.get('schema_version')=='trace-hunter/1.0' else VALIDATOR
    errors=[f"/{'/'.join(map(str,e.absolute_path))}: {e.message}" for e in validator.iter_errors(trace)]
    if errors: raise InvalidTrace(errors)
    observed=trace.get('environment',{}).get('observed_at')
    if observed is not None:
        try:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})',observed):raise ValueError()
            datetime.fromisoformat(observed.upper().replace('Z','+00:00'))
        except ValueError:errors.append('/environment/observed_at: require a timestamp with timezone')
    for key in ('spans','sources','phases','evidence'):
        ids=[x['id'] for x in trace[key]]
        if len(set(ids))!=len(ids):errors.append(f'/{key}: duplicate id')
    spans={s['id']:s for s in trace['spans']}; phases={p['id'] for p in trace['phases']};sources={s['id'] for s in trace['sources']}
    if not any(p['purpose']=='task' for p in trace['phases']):errors.append('/phases: at least one task phase required')
    for item in trace['phases']+trace['spans']:
        for k in ('start_ms','end_ms','duration_ms'):
            n=item.get(k)
            if n is not None and not math.isfinite(n):errors.append(f"/{item['id']}/{k}: must be finite")
        a,b=item.get('start_ms'),item.get('end_ms')
        if a is not None and b is not None and b<a:errors.append(f"/{item['id']}: end precedes start")
    for s in trace['spans']:
        if s['phase_id'] not in phases:errors.append(f"/{s['id']}: unknown phase")
        if s['parent_id'] is not None and s['parent_id'] not in spans:errors.append(f"/{s['id']}: unknown parent")
        if s['kind']=='model' and not s['request_id']:errors.append(f"/{s['id']}: model request_id required")
        if s['kind']!='model' and s['usage'] is not None:errors.append(f"/{s['id']}: usage belongs to a model request")
        u=s['usage']
        if u:
            i,o=u['input_tokens'],u['output_tokens'];r,w=u['cache_read_tokens'],u['cache_write_tokens']
            if i is not None and ((r is not None and r>i) or (w is not None and w>i) or (r is not None and w is not None and r+w>i)):errors.append(f"/{s['id']}: cache is a subset of input")
            if o is not None and u['thinking_tokens'] is not None and u['thinking_tokens']>o:errors.append(f"/{s['id']}: thinking is a subset of output")
        seen=set();p=s['id']
        while p in spans:
            if p in seen:errors.append(f"/{s['id']}: parent cycle");break
            seen.add(p);p=spans[p]['parent_id']
    tool_sequences=[s['sequence'] for s in trace['spans'] if s['kind']=='tool' and 'sequence' in s]
    if len(tool_sequences)!=len(set(tool_sequences)):errors.append('/spans: tool sequence must be unique within run')
    invokers={}
    for e in trace['links']:
        if e['from'] not in spans or e['to'] not in spans:errors.append('/links: missing endpoint')
        elif e['type']=='invokes' and (spans[e['from']]['kind']!='model' or spans[e['to']]['kind']!='tool'):errors.append('/links: invokes must connect model to tool')
        if e['type']=='invokes':
            if e['to'] in invokers and invokers[e['to']]!=e['from']:errors.append('/links: a tool can have only one invoking model span')
            invokers[e['to']]=e['from']
    for e in trace['spans']+trace['evidence']:
        if e['source']['source_id'] not in sources:errors.append('/source: missing source id')
        for sid in e.get('span_ids',[]):
            if sid not in spans:errors.append('/evidence: missing span reference')
    if errors:raise InvalidTrace(errors)
    return trace
