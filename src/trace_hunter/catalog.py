"""Collection and case definitions, separate from immutable execution traces."""
import hashlib
import json
from jsonschema import Draft202012Validator
from .protocol import ROOT, InvalidTrace

SCHEMA = json.loads((ROOT / 'schemas/catalog-v1.schema.json').read_text())
VALIDATOR = Draft202012Validator(SCHEMA)
UNASSIGNED = '__unassigned__'


def canonical(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return payload, hashlib.sha256(payload.encode()).hexdigest()


def validate_catalog(value):
    errors = [f"/{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in VALIDATOR.iter_errors(value)]
    if errors:
        raise InvalidTrace(errors)
    if value['collection']['id'] == UNASSIGNED:
        errors.append('/collection/id: reserved collection id')
    ids = [c['query_id'] for c in value['cases']]
    if len(set(ids)) != len(ids):
        errors.append('/cases: duplicate query_id')
    for case in value['cases']:
        conversation = case['conversation']
        mode, turns = conversation['mode'], conversation['turns']
        if mode == 'single_turn' and len(turns) != 1:
            errors.append(f"/{case['query_id']}: single_turn requires one input turn")
        if mode == 'multi_turn' and len(turns) < 2:
            errors.append(f"/{case['query_id']}: multi_turn requires at least two input turns")
        if mode == 'unknown' and turns:
            errors.append(f"/{case['query_id']}: unknown conversation cannot declare input turns")
        turn_ids = [t['id'] for t in turns]
        if len(set(turn_ids)) != len(turn_ids):
            errors.append(f"/{case['query_id']}: duplicate turn id")
    if errors:
        raise InvalidTrace(errors)
    return value


def case_view(definition, runs):
    if definition is None:
        first = runs[0]
        definition = {'query_id': first['query_id'], 'title': first['title'],
                      'conversation': {'mode': 'unknown', 'turns': []}}
    return {**definition, 'initial_query': runs[0]['query'] if runs else None,
            'run_count': len(runs), 'harnesses': sorted({r['harness'] for r in runs}),
            'models': sorted({r['model'] for r in runs}),
            'environment_count': len({r['env_id'] for r in runs})}
