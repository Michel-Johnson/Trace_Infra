import hashlib
import json
import tempfile
import unittest
import socket
from pathlib import Path
from unittest.mock import Mock

from trace_hunter.content import LocalContentStore
from trace_hunter.database import Repository
from trace_hunter.otlp import OtlpIngestor
from trace_hunter.storage import Store
from trace_hunter.traces.index import TraceIndex
from trace_hunter.uploads import Uploads
from trace_hunter.otlp_grpc import start as start_grpc


class UploadAndOtlpTests(unittest.TestCase):
    def test_resumable_upload_is_idempotent_and_checks_digest(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        jobs = Mock(); jobs.create.return_value = ({"job_id": "job-1"}, False)
        uploads = Uploads(root, jobs, LocalContentStore(root / "content"))
        self.addCleanup(uploads.close)
        raw = b"a" * (1024 * 1024) + b"b" * 17
        value = {"request_key": "upload-1", "source_format": "doubao-export", "size_bytes": len(raw),
                 "sha256": hashlib.sha256(raw).hexdigest(), "part_size": 1024 * 1024,
                 "binding": {"run_id": "r", "query_id": "q", "env_id": "e"},
                 "batch_id": "batch-1", "batch_total": 1}
        status = uploads.create("p1", value)
        for position in reversed(status["missing_parts"]):
            chunk = raw[position * status["part_size"]:(position + 1) * status["part_size"]]
            uploads.put_part("p1", status["upload_id"], position, chunk, hashlib.sha256(chunk).hexdigest())
        completed = uploads.complete("p1", status["upload_id"])
        self.assertEqual((completed["state"], completed["job_id"]), ("completed", "job-1"))
        self.assertEqual(jobs.create.call_args.kwargs["batch_id"], "batch-1")
        self.assertEqual(uploads.create("p1", value)["upload_id"], status["upload_id"])

    def test_otlp_json_preserves_attributes_time_status_and_session(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = Store(root / "db.sqlite", content_store=LocalContentStore(root / "content"))
        self.addCleanup(store.close)
        index = TraceIndex(store.revisions); ingestor = OtlpIngestor(store, index)
        envelope = {"resourceSpans": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "worker"}},
            {"key": "session.id", "value": {"stringValue": "session-1"}},
            {"key": "release", "value": {"stringValue": "2026.09"}}]},
            "scopeSpans": [{"scope": {"name": "test"}, "spans": [{
                "traceId": "0" * 31 + "1", "spanId": "0" * 15 + "1", "name": "model call",
                "startTimeUnixNano": "1000000000", "endTimeUnixNano": "1500000000",
                "status": {"code": 2}, "attributes": [
                    {"key": "gen_ai.system", "value": {"stringValue": "openai"}},
                    {"key": "gen_ai.request.model", "value": {"stringValue": "model-a"}}]}]}]}]}
        raw = json.dumps(envelope).encode(); result = ingestor.ingest("p1", envelope, raw)
        self.assertEqual((len(result["accepted_traces"]), result["accepted_spans"]), (1, 1))
        duplicate = ingestor.ingest("p1", envelope, raw)
        self.assertEqual(duplicate["accepted_traces"], result["accepted_traces"])
        self.assertEqual(store.repository.rows("SELECT count(*) AS n FROM trace_revisions WHERE project_id='p1'")[0]["n"], 1)
        row = store.repository.rows("SELECT status,start_at,end_at,session_id,attributes FROM trace_objects WHERE project_id='p1'")[0]
        self.assertEqual((row["status"], row["session_id"]), ("error", "session-1"))
        self.assertEqual(json.loads(row["attributes"])["gen_ai.request.model"], "model-a")

    def test_otlp_grpc_listener_imports_standard_request(self):
        import grpc
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc
        from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue, InstrumentationScope
        from opentelemetry.proto.resource.v1.resource_pb2 import Resource
        from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = Store(root / "grpc.sqlite", content_store=LocalContentStore(root / "content"))
        self.addCleanup(store.close)
        from trace_hunter.access import Projects
        projects = Projects(store.repository); projects.create_project("grpc-project", "gRPC")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
        server = start_grpc(OtlpIngestor(store, TraceIndex(store.revisions)), projects,
                            address=f"127.0.0.1:{port}")
        self.addCleanup(lambda: server.stop(grace=0))
        request = trace_service_pb2.ExportTraceServiceRequest(resource_spans=[ResourceSpans(
            resource=Resource(attributes=[KeyValue(key="service.name", value=AnyValue(string_value="grpc"))]),
            scope_spans=[ScopeSpans(scope=InstrumentationScope(name="test"), spans=[Span(
                trace_id=b"1" * 16, span_id=b"2" * 8, name="grpc-span",
                start_time_unix_nano=1_000_000_000, end_time_unix_nano=1_100_000_000)])])])
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            response = trace_service_pb2_grpc.TraceServiceStub(channel).Export(
                request, metadata=(("x-trace-project", "grpc-project"),), timeout=5)
        self.assertEqual(response.partial_success.rejected_spans, 0)
        self.assertEqual(store.repository.rows("SELECT count(*) AS n FROM traces WHERE project_id='grpc-project'")[0]["n"], 1)


if __name__ == "__main__":
    unittest.main()
