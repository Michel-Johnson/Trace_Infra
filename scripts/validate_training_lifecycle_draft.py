"""Validate synthetic lifecycle documents; no network, jobs or training are run."""
import argparse
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from build_training_lifecycle_draft import DIRECTORY, content_digest


def key(value):
    return value['kind'], value['id'], value['revision']


def references(value):
    if isinstance(value, dict):
        if set(value) == {'kind', 'id', 'revision', 'digest'}:
            yield value
        else:
            for child in value.values():
                yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)


def validate_flow(bundle):
    if bundle.get('synthetic') is not True or bundle.get('live_execution') is not False:
        raise ValueError('This validator accepts explicitly synthetic offline fixtures only')
    validator = Draft202012Validator(json.loads((DIRECTORY / 'lifecycle.schema.json').read_text()),
                                     format_checker=FormatChecker())
    documents = bundle['documents']
    resources = {}
    for value in [*bundle['external_resources'], *documents]:
        identity = key(value)
        if identity in resources and resources[identity] != value:
            raise ValueError('Immutable resource identity conflict')
        resources[identity] = value
    for value in documents:
        validator.validate(value)
        for ref in references(value):
            target = resources.get(key(ref))
            if target is None or content_digest(target) != ref['digest']:
                raise ValueError('Reference is missing or its digest differs')
        if value['kind'] == 'replay' and value['output_trace_ref'] is not None:
            if value['output_trace_ref']['id'] == value['source_trace_ref']['id']:
                raise ValueError('Replay must produce a new trace identity')
        if value['kind'] == 'validation' and value['verdict'] == 'passed':
            if resources[key(value['replay_ref'])]['state'] != 'completed':
                raise ValueError('Passed validation requires a completed replay in this fixture')
        if value['kind'] == 'dataset':
            snapshot = resources[key(value['snapshot_ref'])]
            for validation_ref in value['validation_refs']:
                validation = resources[key(validation_ref)]
                replay = resources[key(validation['replay_ref'])]
                if replay['source_trace_ref'] not in snapshot['trace_refs']:
                    raise ValueError('Dataset validation belongs to a different input selection')
        if value['kind'] == 'consumption' and value['step_start'] > value['step_end']:
            raise ValueError('Consumption step range is reversed')
    return resources


def ledger_preview(bundle):
    """In-memory receipt semantics only; no database concurrency or source attestation."""
    resources = validate_flow(bundle)
    receipts = {}
    for value in bundle['documents']:
        if value['kind'] != 'consumption':
            continue
        identity = value['consumer_run_id'], value['consumer_attempt'], value['receipt_id']
        if identity in receipts and receipts[identity] != value:
            raise ValueError('Receipt identity reused with different content')
        receipts[identity] = value
    retracted = set()
    for receipt in receipts.values():
        if receipt['event'] != 'retracted':
            continue
        original = resources[key(receipt['original_receipt_ref'])]
        fields = ('consumer_run_id', 'consumer_attempt', 'dataset_ref', 'batch_manifest_ref',
                  'sample_count', 'epoch', 'step_start', 'step_end')
        if original['event'] != 'trainer_committed' or any(receipt[f] != original[f] for f in fields):
            raise ValueError('Retraction must match the original committed receipt scope')
        original_key = key(original)
        if original_key in retracted:
            raise ValueError('Receipt already retracted')
        retracted.add(original_key)
    groups = {}
    for receipt in receipts.values():
        group_key = (receipt['consumer_run_id'], receipt['consumer_attempt'],
                     receipt['dataset_ref']['id'], receipt['dataset_ref']['revision'])
        group = groups.setdefault(group_key, {'consumer_run_id': group_key[0],
            'consumer_attempt': group_key[1], 'dataset_ref': receipt['dataset_ref'],
            'delivered_sample_occurrences': 0, 'committed_sample_occurrences': 0})
        if receipt['event'] == 'delivered':
            group['delivered_sample_occurrences'] += receipt['sample_count']
        elif receipt['event'] == 'trainer_committed' and key(receipt) not in retracted:
            group['committed_sample_occurrences'] += receipt['sample_count']
    return {'synthetic': True, 'live_execution': False, 'source_attestation': 'not_performed',
            'unique_receipts': len(receipts), 'consumers': list(groups.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path, nargs='?', default=DIRECTORY / 'workflow.example.json')
    args = parser.parse_args()
    print(json.dumps(ledger_preview(json.loads(args.file.read_text())), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
