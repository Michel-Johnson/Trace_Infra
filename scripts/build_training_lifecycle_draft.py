"""Generate the offline training lifecycle contract and synthetic reference flow."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / 'contracts/drafts/training-lifecycle-v1'
VERSION = 'trace-hunter/training-lifecycle/0.1-draft'


def obj(properties, required=None):
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required,
            'additionalProperties': False}


def enum(*values):
    return {'enum': list(values)}


def array(item, minimum=0):
    return {'type': 'array', 'items': item, 'minItems': minimum, 'uniqueItems': True}


def link(*kinds, nullable=False):
    value = {'allOf': [{'$ref': '#/$defs/ref'}, {'properties': {'kind': enum(*kinds)}}]}
    return {'anyOf': [value, {'type': 'null'}]} if nullable else value


def document(kind, properties):
    return obj({'schema_version': {'const': VERSION}, 'kind': {'const': kind},
                'id': {'$ref': '#/$defs/id'}, 'revision': {'type': 'integer', 'minimum': 1}, **properties})


def conditional(field, value, properties):
    return {'if': {'properties': {field: {'const': value}}, 'required': [field]},
            'then': {'properties': properties}}


def build_schema():
    identifier = {'type': 'string', 'minLength': 1, 'maxLength': 256}
    digest = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
    count = {'type': 'integer', 'minimum': 0}
    ref = obj({'kind': identifier, 'id': identifier,
               'revision': {'type': 'integer', 'minimum': 1}, 'digest': digest})
    snapshot = document('selection', {
        'resolved_at': {'type': 'string', 'format': 'date-time'},
        'query_digest': digest, 'trace_refs': array(link('trace')),
        'selection_manifest_ref': link('artifact'),
    })
    analysis = document('analysis_request', {
        'snapshot_ref': link('selection'), 'analyzer_ref': link('analyzer'),
        'config_ref': link('config'), 'reuse': enum('exact', 'force_new'),
        'idempotency_key': identifier,
    })
    replay = document('replay', {
        'source_trace_ref': link('trace'), 'executor_ref': link('executor'),
        'mode': enum('recorded_tools', 'reexecute_actions', 'regenerate'),
        'environment_ref': link('environment', nullable=True),
        'initial_state_ref': link('artifact', nullable=True),
        'recording_ref': link('artifact', nullable=True),
        'model_config_ref': link('config', nullable=True),
        'state': enum('queued', 'running', 'completed', 'failed', 'blocked', 'cancelled'),
        'attempt': {'type': 'integer', 'minimum': 1},
        'output_trace_ref': link('trace', nullable=True),
        'issues': array(obj({'code': identifier, 'path': {'type': 'string'},
                             'message': {'type': 'string'}})),
    })
    replay['allOf'] = [
        conditional('mode', 'recorded_tools', {'recording_ref': link('artifact')}),
        *[conditional('mode', mode, {'environment_ref': link('environment'),
                                    'initial_state_ref': link('artifact')})
          for mode in ('reexecute_actions', 'regenerate')],
        conditional('mode', 'regenerate', {'model_config_ref': link('config')}),
        conditional('state', 'completed', {'output_trace_ref': link('trace')}),
        conditional('state', 'blocked', {'issues': {'minItems': 1}}),
    ]
    check = obj({'name': identifier, 'verdict': enum('passed', 'failed', 'unknown', 'not_applicable'),
                 'evidence_refs': array(link('artifact'))})
    check['allOf'] = [conditional('verdict', 'passed', {'evidence_refs': {'minItems': 1}})]
    validation = document('validation', {
        'replay_ref': link('replay'), 'policy_ref': link('policy'),
        'verdict': enum('passed', 'failed', 'insufficient_data', 'not_applicable'),
        'checks': array(check, 1),
    })
    validation['allOf'] = [conditional('verdict', 'passed', {
        'checks': {'items': {'properties': {'verdict': {'const': 'passed'}}}}})]
    dataset = document('dataset', {
        'snapshot_ref': link('selection'), 'sample_manifest_ref': link('artifact'),
        'sample_count': {'type': 'integer', 'minimum': 1},
        'transform_ref': link('transform'), 'profile_ref': link('profile'),
        'release_policy_ref': link('policy'), 'admission_report_ref': link('artifact'),
        'validation_refs': array(link('validation')), 'analysis_refs': array(link('artifact')),
    })
    receipt = document('consumption', {
        'receipt_id': identifier, 'dataset_ref': link('dataset'), 'consumer_run_id': identifier,
        'consumer_attempt': {'type': 'integer', 'minimum': 1},
        'event': enum('delivered', 'trainer_committed', 'retracted'),
        'batch_manifest_ref': link('artifact'), 'sample_count': {'type': 'integer', 'minimum': 1},
        'epoch': count, 'step_start': count, 'step_end': count,
        'commit_evidence_ref': link('artifact', nullable=True),
        'original_receipt_ref': link('consumption', nullable=True),
    })
    receipt['properties']['revision'] = {'const': 1}
    receipt['allOf'] = [
        conditional('event', 'trainer_committed', {'commit_evidence_ref': link('artifact'),
                                                  'original_receipt_ref': {'type': 'null'}}),
        conditional('event', 'delivered', {'commit_evidence_ref': {'type': 'null'},
                                         'original_receipt_ref': {'type': 'null'}}),
        conditional('event', 'retracted', {'original_receipt_ref': link('consumption'),
                                         'commit_evidence_ref': link('artifact')}),
    ]
    definitions = {'id': identifier, 'ref': ref, 'selection': snapshot, 'analysis_request': analysis,
                   'replay': replay, 'validation': validation, 'dataset': dataset, 'consumption': receipt}
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema',
            '$id': 'urn:trace-hunter:training-lifecycle:0.1-draft',
            'description': 'Offline design only; current HTTP API does not accept these objects.',
            'oneOf': [{'$ref': '#/$defs/' + name} for name in definitions if name not in ('id', 'ref')],
            '$defs': definitions}


def content_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def reference(value):
    return {key: value[key] for key in ('kind', 'id', 'revision')} | {'digest': content_digest(value)}


def example():
    # External fixture payloads have real content digests, but represent no real
    # model, environment, execution, validation or training completion.
    external = []

    def resource(kind, name):
        value = {'kind': kind, 'id': name, 'revision': 1,
                 'synthetic': True, 'description': 'Contract fixture; no real execution evidence.'}
        external.append(value)
        return reference(value)

    def make(kind, name, **fields):
        return {'schema_version': VERSION, 'kind': kind, 'id': name, 'revision': 1, **fields}

    source = resource('trace', 'original-trace')
    output = resource('trace', 'replayed-trace')
    selection = make('selection', 'selection-001', resolved_at='2026-09-11T00:00:00Z',
        query_digest=content_digest({'case': 'synthetic-case'}), trace_refs=[source],
        selection_manifest_ref=resource('artifact', 'selected-records'))
    analysis = make('analysis_request', 'analysis-001', snapshot_ref=reference(selection),
        analyzer_ref=resource('analyzer', 'quality-v1'), config_ref=resource('config', 'analysis-config'),
        reuse='exact', idempotency_key='analysis-request-001')
    replay = make('replay', 'replay-001', source_trace_ref=source,
        executor_ref=resource('executor', 'synthetic-runner'), mode='reexecute_actions',
        environment_ref=resource('environment', 'isolated-environment'),
        initial_state_ref=resource('artifact', 'initial-state'), recording_ref=None,
        model_config_ref=None, state='completed', attempt=1, output_trace_ref=output, issues=[])
    validation = make('validation', 'validation-001', replay_ref=reference(replay),
        policy_ref=resource('policy', 'acceptance-v1'), verdict='passed',
        checks=[{'name': 'expected-state', 'verdict': 'passed',
                 'evidence_refs': [resource('artifact', 'state-diff')]}])
    batch = resource('artifact', 'sample-batch')
    dataset = make('dataset', 'dataset-001', snapshot_ref=reference(selection),
        sample_manifest_ref=batch, sample_count=2, transform_ref=resource('transform', 'extract-v1'),
        profile_ref=resource('profile', 'synthetic-training-profile'),
        release_policy_ref=resource('policy', 'release-v1'),
        admission_report_ref=resource('artifact', 'admission-report'),
        validation_refs=[reference(validation)], analysis_refs=[resource('artifact', 'quality-report')])
    common = dict(dataset_ref=reference(dataset), consumer_run_id='training-run-001', consumer_attempt=1,
                  batch_manifest_ref=batch, sample_count=2, epoch=0, step_start=10, step_end=10,
                  original_receipt_ref=None)
    delivery = make('consumption', 'delivery-001', receipt_id='delivery-001',
                    event='delivered', commit_evidence_ref=None, **common)
    committed = make('consumption', 'receipt-001', receipt_id='receipt-001', event='trainer_committed',
                     commit_evidence_ref=resource('artifact', 'trainer-step-commit'), **common)
    return {'synthetic': True, 'live_execution': False, 'external_resources': external,
            'documents': [selection, analysis, replay, validation, dataset, delivery, committed]}


def main():
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    for name, value in [('lifecycle.schema.json', build_schema()), ('workflow.example.json', example())]:
        (DIRECTORY / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    print('Generated offline lifecycle schema and synthetic workflow.')


if __name__ == '__main__':
    main()
