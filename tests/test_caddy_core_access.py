"""Current Core API and Agent boundary through the real Caddy gateway."""

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(os.environ.get("TRACE_HUNTER_TEST_ROOT", Path(__file__).resolve().parents[1]))
CADDY = Path(os.environ.get("CADDY_BINARY", Path.home() / "apps/trace-hunter-tools/caddy"))
CADDYFILE = Path(os.environ.get("CADDYFILE_UNDER_TEST", ROOT / "deploy/Caddyfile"))


def port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


@unittest.skipUnless(CADDY.is_file(), "real Caddy executable is unavailable")
class CoreCaddyAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import uvicorn
        from trace_hunter.storage import Store
        from trace_hunter_api.app import create_app

        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        folder = Path(cls.temp.name)
        cls.environment = patch.dict(os.environ, {
            "TRACE_HUNTER_AGENT_ENABLED": "1",
            "TRACE_HUNTER_AGENT_WORKER_TOKEN": "synthetic-worker-token",
            "TRACE_HUNTER_AGENT_STATE_DIR": str(folder / "agent-state"),
        })
        cls.environment.start()
        cls.addClassCleanup(cls.environment.stop)
        cls.store = Store(folder / "gateway.sqlite")
        cls.addClassCleanup(cls.store.close)
        cls.app = create_app(cls.store)
        cls.app.state.projects.create_project("shared", "Shared")
        cls.api_port, cls.web_port = port(), port()
        cls.api = uvicorn.Server(uvicorn.Config(cls.app, host="127.0.0.1", port=cls.api_port,
                                               log_level="error", access_log=False, proxy_headers=False))
        cls.api_thread = threading.Thread(target=cls.api.run, daemon=True)
        cls.api_thread.start()

        def stop_api():
            cls.api.should_exit = True
            cls.api_thread.join(timeout=5)

        cls.addClassCleanup(stop_api)
        for _ in range(100):
            if cls.api.started:
                break
            time.sleep(0.02)
        if not cls.api.started:
            raise RuntimeError("Core API did not start")

        (folder / "index.html").write_text("trace-hunter-core-test")
        environment = {**os.environ, "SITE_ADDRESS": f"http://127.0.0.1:{cls.web_port}",
                       "WEB_HOST": "127.0.0.1", "WEB_ROOT": str(folder),
                       "API_UPSTREAM": f"127.0.0.1:{cls.api_port}",
                       "TRACE_HUNTER_AUTH_CONFIG": str(ROOT / "deploy/auth-disabled.caddy")}
        cls.log = (folder / "caddy.log").open("wb")
        cls.addClassCleanup(cls.log.close)
        cls.process = subprocess.Popen(
            [str(CADDY), "run", "--config", str(CADDYFILE), "--adapter", "caddyfile"],
            env=environment, stdout=cls.log, stderr=cls.log)

        def stop_caddy():
            cls.process.terminate()
            cls.process.wait(timeout=5)

        cls.addClassCleanup(stop_caddy)
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(100):
            if cls.process.poll() is not None:
                raise RuntimeError("Caddy did not start")
            try:
                if cls.fetch("/")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.02)
        else:
            raise RuntimeError("Caddy gateway did not become ready")

    @classmethod
    def fetch(cls, path, *, method="GET", headers=None, body=None):
        request_headers = dict(headers or {})
        content = json.dumps(body).encode() if body is not None else None
        if content is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"http://127.0.0.1:{cls.web_port}{path}",
                                         data=content, method=method, headers=request_headers)
        try:
            with cls.opener.open(request, timeout=5) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def test_static_site_and_core_api_share_the_entry(self):
        self.assertEqual(self.fetch("/")[:2], (200, b"trace-hunter-core-test"))
        status, content, _ = self.fetch("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["status"], "ok")
        status, content, _ = self.fetch("/api/v1/projects")
        self.assertEqual(status, 200)
        self.assertIn("shared", [item["project_id"] for item in json.loads(content)["items"]])
        self.assertEqual(self.fetch("/api/no-such-route")[0], 404)

    def test_agent_cookie_and_worker_token_stay_in_api(self):
        status, content, headers = self.fetch("/api/v1/agent/capabilities")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["identity"], "browser_guest")
        self.assertIn("HttpOnly", headers.get("Set-Cookie", ""))
        self.assertEqual(self.fetch("/api/v1/agent/worker/claim", method="POST")[0], 403)
        self.assertEqual(self.fetch("/api/v1/agent/worker/claim", method="POST",
                                    headers={"X-Agent-Worker-Token": "wrong"})[0], 403)
        status, content, _ = self.fetch("/api/v1/agent/worker/claim", method="POST",
                                        headers={"X-Agent-Worker-Token": "synthetic-worker-token"})
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(content)["turn"])
        self.assertEqual(self.fetch("/api/v1/agent/sessions", method="POST",
                                    headers={"Origin": "https://untrusted.example"},
                                    body={"project_id": "shared"})[0], 403)


if __name__ == "__main__":
    unittest.main()
