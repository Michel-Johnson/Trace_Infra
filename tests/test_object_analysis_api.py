"""Object analysis HTTP API exposes the complete server-side workflow."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from trace_hunter.storage import Store
from trace_hunter_api.app import create_app
from tests.test_trace_search import availability_document


class EvaluatorIsolationAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(Path(self.directory) / "evaluator.sqlite")
        self.addCleanup(self.store.close)
        operator = create_app(self.store)
        operator.state.projects.create_project("evaluation", "Evaluation")
        document = availability_document("evaluator-run")
        self.store.revisions.append("evaluation", json.dumps(document).encode(), request_key="seed")
        operator.state.trace_index.project("evaluation", "evaluator-run", 1)
        self.token = "test-evaluator-token"
        with patch.dict(os.environ, {
            "TRACE_HUNTER_EVALUATOR_ONLY": "true",
            "TRACE_HUNTER_EVALUATOR_TOKEN_SHA256": hashlib.sha256(self.token.encode()).hexdigest(),
            "TRACE_HUNTER_EVALUATOR_PROJECT": "evaluation",
        }):
            app = create_app(self.store)
        self.client = self.enterContext(TestClient(app, client=("127.0.0.1", 44000)))
        self.auth = {"Authorization": "Bearer " + self.token}

    def test_evaluator_can_only_use_model_context_interfaces(self):
        target = {"run_id": "evaluator-run", "revision": 1, "model_span_id": "model-1"}
        allowed = self.client.post("/api/v1/projects/evaluation/objects/query", headers=self.auth,
            json={"scope": {"mode": "model_context", "visible_to": target},
                  "fields": ["object_id", "object_kind"]})
        self.assertEqual(allowed.status_code, 200, allowed.text)
        search = self.client.post("/api/v1/projects/evaluation/search", headers=self.auth,
            json={"scope": "model_context", "visible_to": target, "query": "库存"})
        self.assertEqual(search.status_code, 200, search.text)
        denied = [
            self.client.post("/api/v1/projects/evaluation/objects/query", headers=self.auth,
                            json={"scope": {"mode": "analysis"}}),
            self.client.post("/api/v1/projects/evaluation/search", headers=self.auth,
                            json={"scope": "analysis", "query": "库存"}),
            self.client.post("/api/v1/projects/evaluation/spans/query", headers=self.auth, json={}),
            self.client.get("/api/v1/projects/evaluation/traces/evaluator-run/revisions/1/content",
                            headers=self.auth),
            self.client.post("/api/v1/projects", headers=self.auth,
                             json={"project_id": "other", "name": "Other"}),
            self.client.post("/api/v1/projects/evaluation/metrics/query",
                             headers=self.auth, json={"query": {"scope": {"mode": "analysis"}}}),
        ]
        self.assertEqual([response.status_code for response in denied], [403] * len(denied))

    def test_evaluator_requires_exact_token_and_project(self):
        self.assertEqual(self.client.get("/api/v1/advanced-query-capabilities").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/advanced-query-capabilities",
                                         headers={"Authorization": "Bearer wrong"}).status_code, 403)
        response = self.client.post("/api/v1/projects/other/objects/query", headers=self.auth,
                                    json={"scope": {"mode": "analysis"}})
        self.assertEqual(response.status_code, 403)

    def test_evaluator_mode_rejects_incomplete_security_configuration(self):
        with patch.dict(os.environ, {"TRACE_HUNTER_EVALUATOR_ONLY": "true",
                                     "TRACE_HUNTER_EVALUATOR_TOKEN_SHA256": "invalid",
                                     "TRACE_HUNTER_EVALUATOR_PROJECT": "evaluation"}):
            with self.assertRaisesRegex(ValueError, "token SHA-256"):
                create_app(self.store)


if __name__ == "__main__":
    unittest.main()
