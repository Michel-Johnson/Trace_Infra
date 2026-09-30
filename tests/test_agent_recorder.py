"""Synthetic tests for private Claude telemetry and body-file discovery."""

import json
import io
import tempfile
import unittest
from pathlib import Path

from trace_hunter.agent_recorder import StreamCapture, body_files, body_index, body_source_id


class AgentRecorderTests(unittest.TestCase):
    def test_stream_budget_records_a_durable_partial_marker(self):
        output = io.BytesIO()
        capture = StreamCapture(limit_bytes=4200)
        self.assertTrue(capture.append(output, {"at": "synthetic", "value": {"type": "init"}}))
        self.assertFalse(capture.append(output, {"at": "synthetic", "value": {
            "type": "assistant", "content": "x" * 500}}))
        self.assertEqual(capture.coverage()["state"], "partial")
        self.assertEqual(capture.coverage()["dropped"], 1)
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["reason"], "stream_budget")

    def test_body_index_accepts_only_files_inside_one_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "body"
            root.mkdir()
            request = root / "synthetic.request.json"
            response = root / "synthetic.response.json"
            request.write_text('{"model":"synthetic"}')
            response.write_text('{"id":"synthetic"}')
            (root / "index.jsonl").write_text(json.dumps({
                "request_file": str(request), "response_file": "synthetic.response.json",
                "message_id": "message-1", "message_uuid": "uuid-1"}) + "\n")
            (root / "escape.request.json").symlink_to(Path(directory) / "outside.json")
            paths = body_files(root)
            self.assertEqual(paths, [request.resolve(), response.resolve()])
            entries, coverage = body_index(root)
            self.assertEqual(coverage["state"], "complete")
            self.assertEqual(entries[0]["request_source_id"], body_source_id(request))
            self.assertEqual(entries[0]["response_source_id"], body_source_id(response))


if __name__ == "__main__":
    unittest.main()
