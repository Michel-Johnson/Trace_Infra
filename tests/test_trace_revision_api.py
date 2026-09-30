"""HTTP revisions use actual bytes, explicit projection and immutable history."""

import asyncio
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "apps/api")]
from scripts.build_api_contract import synthetic_inputs
from trace_hunter.content import LocalContentStore
from trace_hunter.database import Conflict, Repository
from trace_hunter.traces import TraceRevisions
from trace_hunter.traces.index import TraceIndex
from trace_hunter_api import traces
from trace_hunter_api.access import install as install_access
from trace_hunter.access import ServiceIdentities


def reply(value, status=200):
    return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})


class TraceRevisionAPITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.repo = Repository(self.directory / "test.sqlite")
        self.addCleanup(self.repo.close)
        self.content = LocalContentStore(self.directory / "objects")
        self.revisions = TraceRevisions(self.repo, self.content)
        self.index = TraceIndex(self.revisions)
        self.first, _, _ = synthetic_inputs()
        self.run_id = self.first["run"]["id"]
        self.root = "/api/v1/projects/project-one/traces"
        self.versions = self.root + "/" + self.run_id + "/revisions"
        self.app = FastAPI()

        async def error_response(request, error):
            code = 409 if isinstance(error, Conflict) else (
                404 if isinstance(error, KeyError) else (
                    error.status_code if isinstance(error, HTTPException) else 422))
            message = str(error.detail) if isinstance(error, HTTPException) else "Invalid request"
            return reply({"error": message, "details": []}, code)

        for kind in (Conflict, ValueError, KeyError, HTTPException, RequestValidationError):
            self.app.add_exception_handler(kind, error_response)
        traces.install(self.app, self.revisions, self.index, reply)
        install_access(self.app, ServiceIdentities(self.repo), reply)
        self.client = TestClient(self.app, raise_server_exceptions=False, client=('127.0.0.1', 50000))
        self.addCleanup(self.client.close)

    def raw(self, title=None):
        trace = copy.deepcopy(self.first)
        if title is not None:
            trace["run"]["title"] = title
        return b" \n" + json.dumps(trace, ensure_ascii=False, indent=2).encode() + b"\n\t"

    def upload(self, raw=None, key="first", **params):
        return self.client.post(self.root, content=self.raw() if raw is None else raw,
                                headers={"Content-Type": "application/json", "Idempotency-Key": key},
                                params=params)

    def test_original_bytes_round_trip_and_typed_index_without_analysis(self):
        original = self.raw("保留 Unicode、缩进和尾部空白")
        response = self.upload(original)
        self.assertEqual(response.status_code, 201, response.text)
        result = response.json()
        self.assertTrue(result["created"])
        self.assertEqual(result["revision"]["content"]["size_bytes"], len(original))
        self.assertEqual(result["index"]["state"], "complete")
        self.assertEqual(result["index"]["counts"]["records"], len(self.first["spans"]))
        traces.TraceRevisionAccepted.model_validate(result)
        fetched = self.client.get(self.versions + "/1/content")
        self.assertEqual(fetched.content, original)
        self.assertEqual(fetched.headers["content-type"], "application/json")
        self.assertEqual(fetched.headers["cache-control"], "no-store")
        self.assertEqual(self.repo.rows("SELECT count(*) AS n FROM analysis_results")[0]["n"], 0)

    def test_idempotency_header_conflicts_and_retry_does_not_rebuild(self):
        missing = self.client.post(self.root, content=self.raw(), headers={"Content-Type": "application/json"})
        self.assertEqual(missing.status_code, 422)
        self.assertEqual(self.upload(key=" ").status_code, 422)
        first = self.upload()
        self.assertEqual(first.status_code, 201)
        with patch.object(self.index, "project", side_effect=AssertionError("must not reproject")) as project:
            repeated = self.upload()
        self.assertEqual(repeated.status_code, 200)
        project.assert_not_called()
        self.assertFalse(repeated.json()["created"])
        self.assertEqual(first.json()["revision"], repeated.json()["revision"])
        self.assertEqual(self.upload(self.raw() + b" ").status_code, 409)
        self.assertEqual(self.upload(expected_previous=1).status_code, 409)
        self.assertEqual(self.repo.rows("SELECT count(*) AS n FROM trace_revisions")[0]["n"], 1)

    def test_version_updates_preserve_old_bytes_and_paginate_descending(self):
        documents = [self.raw("revision " + str(n)) for n in (1, 2, 3)]
        for revision, raw in enumerate(documents, 1):
            response = self.upload(raw, key=str(revision), expected_previous=revision - 1, derivation="supplement")
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["revision"]["revision"], revision)
        self.assertEqual(self.upload(self.raw("stale"), key="stale", expected_previous=1).status_code, 409)
        for revision, raw in enumerate(documents, 1):
            self.assertEqual(self.client.get(self.versions + f"/{revision}/content").content, raw)
        page = self.client.get(self.versions, params={"limit": 2}).json()
        self.assertEqual([item["revision"] for item in page["items"]], [3, 2])
        self.assertEqual(page["next_before"], 2)
        final = self.client.get(self.versions, params={"before": page["next_before"], "limit": 2}).json()
        self.assertEqual([item["revision"] for item in final["items"]], [1])
        self.assertIsNone(final["next_before"])
        for params in ({"limit": 101}, {"limit": 0}, {"before": 0}):
            self.assertEqual(self.client.get(self.versions, params=params).status_code, 422)

    def test_opaque_v2_run_id_is_readable_on_every_route(self):
        from urllib.parse import quote
        document = json.loads((ROOT / "examples/drafts/trace-v2/minimal-partial.json").read_text())
        document["run"]["id"] = "provider/session/revisions/detail"
        raw = json.dumps(document, ensure_ascii=False).encode()
        result = self.upload(raw, key="opaque-id")
        self.assertEqual(result.status_code, 201, result.text)
        versions = self.root + "/" + quote(document["run"]["id"], safe="") + "/revisions"
        self.assertEqual(self.client.get(versions).status_code, 200)
        self.assertEqual(self.client.get(versions + "/1").status_code, 200)
        self.assertEqual(self.client.get(versions + "/1/content").content, raw)
        self.assertEqual(self.client.post(versions + "/1/index").status_code, 200)

    def test_legacy_import_derivation_is_reserved_for_the_compatibility_bridge(self):
        self.assertEqual(self.upload(derivation="legacy_import").status_code, 422)

    def test_read_routes_do_not_create_index_or_analysis(self):
        self.revisions.append("project-one", self.raw(), request_key="direct")
        with patch.object(self.index, "project", side_effect=AssertionError("read must not project")) as project:
            with patch.object(self.revisions, "read", side_effect=AssertionError("metadata must not read bytes")) as read:
                response = self.client.get(self.versions + "/1")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["index"]["state"], "unindexed")
                self.assertTrue(all(value is None for value in response.json()["index"]["counts"].values()))
                self.assertEqual(self.client.get(self.versions).status_code, 200)
            read.assert_not_called()
            self.assertEqual(self.client.get(self.versions + "/1/content").content, self.raw())
        project.assert_not_called()
        self.assertEqual(self.repo.rows("SELECT projection_state FROM trace_revisions")[0]["projection_state"], "unindexed")
        self.assertEqual(self.repo.rows("SELECT count(*) AS n FROM analysis_results")[0]["n"], 0)
        rebuilt = self.client.post(self.versions + "/1/index")
        self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
        self.assertEqual(rebuilt.json()["index"]["state"], "complete")

    def test_corrupt_content_fails_read_and_explicit_rebuild_marks_failed(self):
        descriptor = self.upload().json()["revision"]
        digest = descriptor["content"]["digest"].split(":", 1)[1]
        file = self.directory / "objects" / "sha256" / digest[:2] / digest[2:]
        file.write_bytes(b"secret-corrupt-body")
        read = self.client.get(self.versions + "/1/content")
        self.assertEqual(read.status_code, 500, read.text)
        self.assertNotIn("secret-corrupt-body", read.text)
        self.assertNotIn(str(file), read.text)
        metadata = self.client.get(self.versions + "/1")
        self.assertEqual(metadata.status_code, 200)
        self.assertEqual(metadata.json()["index"]["state"], "complete")
        rebuilt = self.client.post(self.versions + "/1/index")
        self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
        self.assertEqual(rebuilt.json()["index"]["state"], "failed")
        self.assertEqual(rebuilt.json()["index"]["error_code"], "content_integrity_failed")
        self.assertTrue(all(value is None for value in rebuilt.json()["index"]["counts"].values()))
        file.unlink()
        self.assertEqual(self.client.get(self.versions + "/1/content").status_code, 500)

    def test_projection_failure_keeps_accepted_revision_and_can_be_retried_explicitly(self):
        with patch.object(self.index, "_project", side_effect=RuntimeError("private-projection-error")):
            response = self.upload()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["index"]["state"], "failed")
        self.assertNotIn("private-projection-error", response.text)
        self.assertEqual(self.client.get(self.versions + "/1/content").content, self.raw())
        self.assertEqual(self.client.get(self.versions + "/1").json()["index"]["state"], "failed")
        self.assertEqual(self.client.post(self.versions + "/1/index").json()["index"]["state"], "complete")
        with patch.object(self.index, "project", side_effect=RuntimeError("index database unavailable")):
            response = self.upload(self.raw("second"), key="second", expected_previous=1)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["index"]["error_code"], "projection_failed")
        self.assertEqual(self.client.get(self.versions + "/2/content").content, self.raw("second"))
        self.assertNotIn("index database unavailable", response.text)

    def test_content_type_and_actual_stream_byte_limit(self):
        self.assertEqual(traces.MAX_BYTES, 16 * 1024 * 1024)
        for media_type in (None, "text/plain", "application/json-evil", "application/ld+json"):
            headers = {"Idempotency-Key": "media"}
            if media_type is not None:
                headers["Content-Type"] = media_type
            response = self.client.post(self.root, content=self.raw(), headers=headers)
            self.assertEqual(response.status_code, 415, response.text)
        with patch.object(traces, "MAX_BYTES", len(self.raw())):
            response = self.client.post(self.root, content=self.raw(), headers={
                "Content-Type": "Application/JSON; charset=utf-8", "Idempotency-Key": "boundary"})
            self.assertEqual(response.status_code, 201, response.text)
            response = self.client.post(self.root, content=iter([self.raw(), b" "]), headers={
                "Content-Type": "application/json", "Idempotency-Key": "overflow", "Content-Length": "1"})
            self.assertEqual(response.status_code, 413, response.text)

        async def chunks():
            yield b"123"
            yield b"456"
            raise AssertionError("body must stop before reading any further chunks")

        with patch.object(traces, "MAX_BYTES", 5):
            request = SimpleNamespace(headers={"content-type": "application/json"}, stream=chunks)
            with self.assertRaises(HTTPException) as error:
                asyncio.run(traces.document_bytes(request))
            self.assertEqual(error.exception.status_code, 413)

    def test_invalid_json_revision_parameters_and_project_isolation(self):
        for raw in (b"{}", b"[]", b"not-json", b'{"schema_version":1,"schema_version":2}'):
            self.assertEqual(self.upload(raw).status_code, 422)
        self.assertEqual(self.upload(expected_previous=-1).status_code, 422)
        self.assertEqual(self.upload(derivation="automatic-rewrite").status_code, 422)
        self.assertEqual(self.repo.rows("SELECT count(*) AS n FROM trace_revisions")[0]["n"], 0)
        self.assertEqual(self.upload().status_code, 201)
        self.assertEqual(self.client.get(self.versions.replace("project-one", "project-two") + "/1").status_code, 404)
        self.assertEqual(self.client.get(self.versions + "/99").status_code, 404)
        self.assertEqual(self.client.get(self.versions + "/0").status_code, 422)

    def test_v2_draft_preserves_experimental_status_and_never_fetches_source_locator(self):
        trace = json.loads((ROOT / "examples/drafts/trace-v2/multiturn-resume.json").read_bytes())
        trace["sources"][0]["locator"] = "https://must-not-be-fetched.invalid/private-source"
        raw = json.dumps(trace, ensure_ascii=False, indent=2).encode() + b"\n"
        with patch("urllib.request.urlopen", side_effect=AssertionError("must not fetch sources")) as fetch:
            response = self.upload(raw, key="v2")
        fetch.assert_not_called()
        self.assertEqual(response.status_code, 201, response.text)
        descriptor = response.json()["revision"]
        self.assertEqual(descriptor["format_version"], "trace-hunter/2.0-draft.1")
        self.assertEqual(descriptor["metadata"]["format_stability"], "experimental")
        self.assertEqual(descriptor["metadata"]["source_verification"], "document_bytes_only")
        self.assertEqual(descriptor["metadata"]["source_document"], trace["document"])
        self.assertEqual(descriptor["metadata"]["environment"], trace["run"]["environment"])
        self.assertEqual(response.json()["index"]["state"], "complete")
        self.assertEqual(self.client.get(self.root + "/multiturn-resume/revisions/1/content").content, raw)

    def test_openapi_has_precise_responses_header_and_raw_body_refs(self):
        spec = self.app.openapi()
        path = "/api/v1/projects/{project_id}/traces"
        operation = spec["paths"][path]["post"]
        self.assertEqual(operation["requestBody"]["content"]["application/json"]["schema"]["oneOf"], [
            {"$ref": "#/components/schemas/TraceInput"}, {"$ref": "#/components/schemas/TraceV2Draft"}])
        header = next(p for p in operation["parameters"] if p["name"] == "Idempotency-Key")
        self.assertTrue(header["required"])
        self.assertEqual(header["in"], "header")
        for code in ("200", "201"):
            self.assertEqual(operation["responses"][code]["content"]["application/json"]["schema"],
                             {"$ref": "#/components/schemas/TraceRevisionAccepted"})
        schema = spec["components"]["schemas"]["TraceIndexCounts"]
        self.assertEqual(set(schema["required"]), {"records", "models", "model_batches", "tools", "agents", "waits", "other", "unknown"})
        self.assertFalse(schema["additionalProperties"])

    def test_invalid_internal_response_is_not_reported_as_client_error(self):
        self.assertEqual(self.upload().status_code, 201)
        with patch.object(self.index, "status", return_value={"private_value": "secret-internal-input"}):
            response = self.client.get(self.versions + "/1")
        self.assertEqual(response.status_code, 500, response.text)
        self.assertNotIn("secret-internal-input", response.text)


if __name__ == "__main__":
    unittest.main()
