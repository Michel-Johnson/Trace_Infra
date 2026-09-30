"""Contract checks for independent traces, turns and optional evaluation binding."""
import copy
import json
import sys
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from trace_hunter import contract_preview as trace
from trace_hunter import platform_preview as platform

IMPORT_SCHEMA = json.loads((ROOT / "contracts/drafts/import-v1/import.schema.json").read_text())
REGISTRY = Registry().with_resource(trace.SCHEMA["$id"], Resource.from_contents(trace.SCHEMA))
VALIDATOR = Draft202012Validator(IMPORT_SCHEMA, registry=REGISTRY)


def fixture(name):
    return json.loads((ROOT / "examples/drafts/trace-v2" / (name + ".json")).read_text())


def batch(*documents):
    return {"schema_version": "trace-hunter-import/1.0-draft.1", "items": [
        {"item_id": str(index), "trace": document} for index, document in enumerate(documents)
    ]}


class NeutralImportTests(unittest.TestCase):
    def test_unbound_trace_accepts_absent_or_null_query_and_environment_ids(self):
        document = fixture("minimal-partial")
        for key in ("query_id", "env_id"):
            document["run"].pop(key)
        trace.validate(document)
        VALIDATOR.validate(batch(document))
        document["run"].update(query_id=None, env_id=None)
        trace.validate(document)
        VALIDATOR.validate(batch(document))

    def test_multiple_turns_remain_one_trace(self):
        document = fixture("multiturn-resume")
        VALIDATOR.validate(batch(document))
        self.assertEqual(len(batch(document)["items"]), 1)
        self.assertEqual(len(document["turns"]), 2)
        self.assertGreater(len(document["spans"]), len(document["turns"]))
        trace.validate(document)

    def test_batch_can_mix_queries_without_merging_record_namespaces(self):
        first = fixture("minimal-partial")
        second = copy.deepcopy(first)
        second["run"].update(id="another-run", query_id="another-query")
        second["document"]["id"] = "another-document"
        value = batch(first, second)
        VALIDATOR.validate(value)
        for item in value["items"]:
            trace.validate(item["trace"])
        self.assertEqual(first["spans"][0]["id"], second["spans"][0]["id"])
        self.assertNotEqual(first["run"]["query_id"], second["run"]["query_id"])

    def test_import_needs_neither_catalog_nor_plugin(self):
        value = batch(fixture("minimal-partial"))
        VALIDATOR.validate(value)
        self.assertEqual(set(value), {"schema_version", "items"})
        value["plugin"] = {"execute": True}
        self.assertTrue(list(VALIDATOR.iter_errors(value)))

    def test_invalid_trace_is_located_to_its_batch_item(self):
        value = batch(fixture("minimal-partial"), fixture("multiturn-resume"))
        value["items"][1]["trace"]["spans"][0]["status"] = "invalid-status"
        errors = list(VALIDATOR.iter_errors(value))
        self.assertTrue(errors)
        self.assertTrue(all(list(error.absolute_path)[:2] == ["items", 1] for error in errors))

    def test_unbound_platform_metadata_keeps_source_document_identity(self):
        value = json.loads((ROOT / "examples/drafts/platform-v1/run-complete-declaration.json").read_text())
        original = copy.deepcopy(value["trace_ref"])
        value.update(query_id=None, env_id=None, case_ref=None, environment_ref=None, benchmark_ref=None, experiment_ref=None)
        value["environment_match"] = {"status": "unverified", "evidence_refs": [], "reason": "No benchmark environment is bound."}
        platform.validate(value)
        self.assertEqual(value["trace_ref"], original)
        value["case_ref"] = {"id": "some-case", "revision": "1", "digest": "0" * 64}
        with self.assertRaisesRegex(ValueError, "CASE_BINDING_CONFLICT"):
            platform.validate(value)

    def test_schema_valid_and_empty_batch_rejected(self):
        Draft202012Validator.check_schema(IMPORT_SCHEMA)
        self.assertTrue(list(VALIDATOR.iter_errors(batch())))


if __name__ == "__main__":
    unittest.main()
