"""Local compatibility HTTP server over the consolidated core schema."""

import http.client
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from scripts.build_api_contract import make_spec, synthetic_inputs
from trace_hunter.server import make_handler
from trace_hunter.storage import Store
from trace_hunter_api.app import create_app


class APIContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / "core.sqlite"); self.addCleanup(self.store.close)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True); thread.start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.trace = synthetic_inputs()[0]

    def request(self, method, path, value=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        raw = None if value is None else json.dumps(value, ensure_ascii=False).encode()
        headers = {} if raw is None else {"Content-Type": "application/json", "Content-Length": str(len(raw))}
        connection.request(method, path, raw, headers); response = connection.getresponse()
        body = json.loads(response.read()); connection.close(); return response.status, body

    def test_trace_import_browse_and_analysis_use_only_core_tables(self):
        status, imported = self.request("POST", "/api/import", self.trace)
        self.assertEqual(status, 201); self.assertTrue(imported["created"])
        self.assertEqual(self.request("POST", "/api/import", self.trace)[0], 200)
        status, bundle = self.request("GET", "/api/runs/" + self.trace["run"]["id"])
        self.assertEqual(status, 200); self.assertEqual(bundle["trace"], self.trace)
        self.assertEqual(self.store.repository.rows("SELECT count(*) AS n FROM analysis_results")[0]["n"], 0)
        self.assertEqual(self.request("POST", "/api/runs/" + self.trace["run"]["id"] + "/analysis")[0], 200)
        self.assertEqual(self.store.repository.rows("SELECT count(*) AS n FROM analysis_results")[0]["n"], 1)

    def test_catalog_import_is_explicitly_removed(self):
        status, body = self.request("POST", "/api/import", {"schema_version": "trace-hunter/catalog/1.0"})
        self.assertEqual(status, 422); self.assertIn("不再存储评测集合", body["error"])

    def test_published_contract_contains_only_core_routes(self):
        published = json.loads((Path(__file__).resolve().parents[1] / "contracts/openapi.json").read_text())
        self.assertEqual(published, make_spec())
        self.assertEqual(published["info"]["version"], "4.0.0")
        self.assertNotIn("/api/v1/principals/{principal_id}", published["paths"])
        self.assertNotIn("ServiceBearer", published["components"]["securitySchemes"])

    def test_published_contract_matches_fastapi_public_routes(self):
        app = create_app(self.store)
        try:
            runtime = {(route.path.replace(":path}", "}"), method.lower()) for route in app.routes
                       if route.path.startswith(("/api/", "/v1/"))
                       for method in (route.methods or set()) if method not in {"HEAD", "OPTIONS"}}
            published = json.loads((Path(__file__).resolve().parents[1] / "contracts/openapi.json").read_text())
            contract = {(path, method) for path, value in published["paths"].items()
                        for method in value if method in {"get", "post", "put", "patch", "delete"}}
            self.assertEqual(runtime, contract)
        finally:
            app.state.batch_operations.close()


if __name__ == "__main__":
    unittest.main()
