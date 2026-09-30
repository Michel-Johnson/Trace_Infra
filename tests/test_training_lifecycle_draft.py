"""Offline contract boundaries; these tests never replay or train a model."""
import copy
import json
from pathlib import Path
import sys
import unittest

from jsonschema import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from build_training_lifecycle_draft import DIRECTORY, build_schema, example, reference
from validate_training_lifecycle_draft import ledger_preview, validate_flow


class TrainingLifecycleDraftTests(unittest.TestCase):
    def setUp(self):
        self.bundle = example()

    def document(self, kind):
        return next(value for value in self.bundle['documents'] if value['kind'] == kind)

    def committed(self):
        return next(value for value in self.bundle['documents']
                    if value.get('event') == 'trainer_committed')

    def test_generated_contract_and_example_are_current(self):
        self.assertEqual(json.loads((DIRECTORY/'lifecycle.schema.json').read_text()), build_schema())
        self.assertEqual(json.loads((DIRECTORY/'workflow.example.json').read_text()), self.bundle)
        validate_flow(self.bundle)

    def test_read_or_delivery_does_not_count_as_training(self):
        self.bundle['documents'].remove(self.committed())
        preview = ledger_preview(self.bundle)
        self.assertEqual(preview['consumers'][0]['committed_sample_occurrences'], 0)
        self.assertEqual(preview['consumers'][0]['delivered_sample_occurrences'], 2)
        self.document('consumption')['event'] = 'read'
        with self.assertRaises(ValidationError):
            validate_flow(self.bundle)

    def test_retransmission_does_not_double_count(self):
        self.bundle['documents'].append(copy.deepcopy(self.committed()))
        preview = ledger_preview(self.bundle)
        self.assertEqual(preview['unique_receipts'], 2)
        self.assertEqual(preview['consumers'][0]['committed_sample_occurrences'], 2)

    def test_same_receipt_with_changed_content_conflicts(self):
        changed = copy.deepcopy(self.committed())
        changed['sample_count'] = 3
        self.bundle['documents'].append(changed)
        with self.assertRaisesRegex(ValueError, 'identity conflict'):
            ledger_preview(self.bundle)

    def test_independent_trainers_and_epochs_can_reuse_samples(self):
        second = copy.deepcopy(self.committed())
        second.update(id='receipt-002', consumer_run_id='training-run-002')
        third = copy.deepcopy(self.committed())
        third.update(id='receipt-003', receipt_id='receipt-003', epoch=1, step_start=20, step_end=20)
        self.bundle['documents'].extend([second, third])
        values = {r['consumer_run_id']: r['committed_sample_occurrences']
                  for r in ledger_preview(self.bundle)['consumers']}
        self.assertEqual(values, {'training-run-001': 4, 'training-run-002': 2})

    def test_committed_requires_training_evidence(self):
        self.committed()['commit_evidence_ref'] = None
        with self.assertRaises(ValidationError):
            ledger_preview(self.bundle)

    def test_replay_requires_environment_and_a_new_trace(self):
        replay = self.document('replay')
        replay['environment_ref'] = None
        with self.assertRaises(ValidationError):
            validate_flow(self.bundle)
        self.bundle = example()
        replay = self.document('replay')
        replay['output_trace_ref'] = replay['source_trace_ref']
        with self.assertRaisesRegex(ValueError, 'new trace'):
            validate_flow(self.bundle)

    def test_regeneration_requires_model_configuration(self):
        self.document('replay')['mode'] = 'regenerate'
        with self.assertRaises(ValidationError):
            validate_flow(self.bundle)

    def test_analysis_is_bound_to_exact_selection(self):
        self.document('analysis_request')['snapshot_ref']['digest'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'digest differs'):
            validate_flow(self.bundle)

    def test_empty_query_snapshot_is_valid(self):
        snapshot = self.document('selection')
        snapshot['trace_refs'] = []
        self.bundle['documents'] = [snapshot]
        preview = ledger_preview(self.bundle)
        self.assertEqual(preview['consumers'], [])

    def test_passed_validation_cannot_hide_unknown_checks(self):
        self.document('validation')['checks'][0]['verdict'] = 'unknown'
        with self.assertRaises(ValidationError):
            validate_flow(self.bundle)

    def test_retraction_preserves_receipts_and_removes_count(self):
        retract = copy.deepcopy(self.committed())
        retract.update(id='retraction-001', receipt_id='retraction-001', event='retracted',
                       original_receipt_ref=reference(self.committed()))
        self.bundle['documents'].append(retract)
        preview = ledger_preview(self.bundle)
        self.assertEqual(preview['unique_receipts'], 3)
        self.assertEqual(preview['consumers'][0]['committed_sample_occurrences'], 0)
        retract['consumer_run_id'] = 'another-trainer'
        with self.assertRaisesRegex(ValueError, 'scope'):
            ledger_preview(self.bundle)

    def test_new_resource_id_does_not_evade_receipt_idempotency(self):
        changed = copy.deepcopy(self.committed())
        changed.update(id='different-resource', sample_count=3)
        self.bundle['documents'].append(changed)
        with self.assertRaisesRegex(ValueError, 'Receipt identity reused'):
            ledger_preview(self.bundle)


if __name__ == '__main__':
    unittest.main()
