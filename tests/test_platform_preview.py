"""Independent failure cases for the platform/plugin contract, offline only."""
import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from trace_hunter import platform_preview as contract

spec = importlib.util.spec_from_file_location("build_platform_contract", ROOT / "scripts/build_platform_contract.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class PlatformPreviewTests(unittest.TestCase):
    def setUp(self):
        self.examples = builder.examples()
        self.run = self.examples["run-complete-declaration"]
        self.plugin = self.examples["plugin-task-success"]
        self.result = self.examples["result-evaluated"]

    def test_generated_contract_and_all_examples(self):
        contract.Draft202012Validator.check_schema(contract.SCHEMA)
        self.assertEqual(builder.build(), contract.SCHEMA)
        for name, example in self.examples.items():
            with self.subTest(name=name):
                self.assertEqual(example, json.loads((ROOT / "examples/drafts/platform-v1" / (name + ".json")).read_text()))
                contract.validate(example)

    def test_complete_and_missing_artifacts_have_different_admission(self):
        self.assertEqual(contract.preflight(self.plugin, self.run)["decision"], "eligible")
        missing = contract.preflight(self.plugin, self.examples["run-missing-artifacts"])
        self.assertEqual(missing["decision"], "insufficient_data")
        self.assertEqual(missing["issues"][0]["path"], "/coverage/artifacts")

    def test_missing_data_never_becomes_a_zero_score(self):
        value = copy.deepcopy(self.examples["result-insufficient-data"])
        contract.validate(value)
        value["metrics"][0]["value"] = 0
        with self.assertRaises(ValueError):
            contract.validate(value)
        value["metrics"][0]["value"] = float("nan")
        with self.assertRaisesRegex(ValueError, "PLATFORM_JSON_INVALID"):
            contract.validate(value)

    def test_confirmed_empty_is_complete_but_unknown_is_not(self):
        self.plugin["requirements"][0]["domain"] = "events"
        self.assertEqual(contract.preflight(self.plugin, self.run)["decision"], "eligible")
        self.run["coverage"]["events"].update(state="unknown", expected_count=None, reason="not_observed")
        self.run["gaps"] = [{"domain": "events", "record_id": None, "path": "/events", "state": "unknown", "reason": "not_observed", "detail": "Unknown whether events occurred."}]
        self.assertEqual(contract.preflight(self.plugin, self.run)["decision"], "insufficient_data")

    def test_partial_policy_does_not_permit_completely_missing_data(self):
        self.plugin["requirements"][0]["on_missing"] = "allow_partial"
        partial = self.examples["run-missing-artifacts"]
        self.assertEqual(contract.preflight(self.plugin, partial)["decision"], "insufficient_data")
        partial["coverage"]["artifacts"].update(state="partial", recorded_count=1, expected_count=2)
        partial["gaps"][0]["state"] = "partial"
        self.assertEqual(contract.preflight(self.plugin, partial)["decision"], "partial")

    def test_partial_counts_and_missing_gap_cannot_claim_complete(self):
        self.run["coverage"]["artifacts"]["expected_count"] = 3
        with self.assertRaisesRegex(ValueError, "COMPLETE_COVERAGE"):
            contract.validate(self.run)
        value = self.examples["run-missing-artifacts"]
        value["gaps"] = []
        with self.assertRaisesRegex(ValueError, "COVERAGE_GAP_REQUIRED"):
            contract.validate(value)

    def test_published_environment_must_be_defined(self):
        self.examples["environment"]["isolation"] = "unknown"
        with self.assertRaises(ValueError):
            contract.validate(self.examples["environment"])
        self.run["environment_match"]["evidence_refs"] = []
        with self.assertRaisesRegex(ValueError, "ENVIRONMENT_MATCH_EVIDENCE_REQUIRED"):
            contract.validate(self.run)

    def test_repeated_runs_need_consistent_seed_plan(self):
        self.examples["benchmark"]["execution_policy"]["seeds"] = [1]
        with self.assertRaisesRegex(ValueError, "REPEAT_SEED"):
            contract.validate(self.examples["benchmark"])

    def test_result_cannot_change_input_or_plugin_identity(self):
        contract.validate_result(self.result, self.plugin, self.result["input_refs"])
        changed = copy.deepcopy(self.result["input_refs"])
        changed[0]["digest"] = "1" * 64
        with self.assertRaisesRegex(ValueError, "RESULT_INPUT_CONFLICT"):
            contract.validate_result(self.result, self.plugin, changed)
        self.result["plugin_ref"]["revision"] = "other"
        with self.assertRaisesRegex(ValueError, "PLUGIN_VERSION_CONFLICT"):
            contract.validate_result(self.result, self.plugin, self.result["input_refs"])

    def test_result_types_aggregation_and_evidence_are_checked(self):
        self.result["metrics"][0]["value"] = "true"
        with self.assertRaisesRegex(ValueError, "METRIC_TYPE_CONFLICT"):
            contract.validate_result(self.result, self.plugin, self.result["input_refs"])
        self.result["metrics"][0]["evidence"][0]["document_id"] = "unassigned-document"
        with self.assertRaisesRegex(ValueError, "EVIDENCE_OUTSIDE_INPUT_SCOPE"):
            contract.validate(self.result)
        self.plugin["metrics"][0]["aggregation"] = "mean"
        with self.assertRaisesRegex(ValueError, "METRIC_AGGREGATION_INVALID"):
            contract.validate(self.plugin)

    def test_versions_labels_extensions_are_separate_from_core(self):
        self.run["metadata"]["extensions"]["vendor.hardware"] = {"accelerator": "example"}
        self.run["metadata"]["labels"]["branch"] = "dev"
        contract.validate(self.run)
        self.run["metadata"]["extensions"]["arbitrary_core_override"] = True
        with self.assertRaises(ValueError):
            contract.validate(self.run)


if __name__ == "__main__":
    unittest.main()
