"""HTTP control plane for the phase-0 APIs. Standard library only.

Paths under /v1 are the trace contract. Paths under /sandboxes are E2B-shaped
aliases: create, snapshot/pause, fork. Restore always returns a new
sandbox_id; this pack does not roll back in place.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .store import TraceError
from .trace import Runtime

_NOT_FOUND = {"StateNotFound", "SpanNotFound", "SandboxGone"}
_CONFLICT = {"ReplayDivergence"}


def _status_for(err: TraceError, *, partial: bool = False) -> int:
    if err.code in _NOT_FOUND:
        return 404
    if err.code in _CONFLICT:
        return 409
    if err.code == "VmmFailed":
        return 503
    return 400


class TraceHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _runtime(self) -> Runtime:
        return self.server.runtime  # type: ignore[attr-defined]

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        if not raw:
            return {}
        return json.loads(raw.decode())

    def _send(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, e: TraceError, *, code: int | None = None) -> None:
        self._send(code if code is not None else _status_for(e), e.as_dict())

    def do_GET(self) -> None:
        try:
            self._handle("GET")
        except TraceError as e:
            self._err(e)
        except Exception as e:
            self._send(500, {"error": "Internal", "message": str(e)})

    def do_POST(self) -> None:
        try:
            self._handle("POST")
        except TraceError as e:
            self._err(e)
        except Exception as e:
            self._send(500, {"error": "Internal", "message": str(e)})

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        q = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        body = self._read_json() if method == "POST" else {}
        rt = self._runtime()

        if method == "GET" and parts == ["health"]:
            self._send(200, {"ok": True})
            return

        if method == "GET" and parts == ["v1", "analytics", "events"]:
            rows = rt.analytics.query_events(
                run_id=q.get("run_id"), span_id=q.get("span_id"),
                action=q.get("action"))
            self._send(200, {"events": rows})
            return

        if method == "GET" and len(parts) == 3 and parts[0] == "v1" and parts[1] == "states":
            self._send(200, rt.get_state(parts[2]))
            return

        if method == "GET" and len(parts) == 3 and parts[0] == "v1" and parts[1] == "spans":
            self._send(200, rt.get_span(parts[2]))
            return

        if method == "POST" and parts == ["v1", "runs"]:
            out = rt.start_run(
                str(body.get("task_id") or ""),
                int(body.get("seed") or 0),
                backend=str(body.get("backend") or "episode"))
            self._send(201, out)
            return

        if method == "POST" and len(parts) == 4 and parts[0] == "v1" and parts[1] == "sandboxes":
            sid, action = parts[2], parts[3]
            if action == "act":
                self._send(200, rt.act(sid, str(body.get("name") or ""),
                                       body.get("params") or {}))
                return
            if action == "commit":
                self._send(201, rt.commit_state(sid, prompt=body.get("prompt")))
                return
            if action == "replay":
                self._send(200, rt.replay_span(
                    sid, str(body.get("span_id") or ""),
                    int(body["stop_before_t"])))
                return
            if action == "branch":
                out = rt.branch(sid, int(body.get("n") or 1))
                failed = any("error" in c for c in out["children"])
                self._send(207 if failed else 201, out)
                return

        if method == "POST" and len(parts) == 4 and parts[0] == "v1" and parts[1] == "states" and parts[3] == "restore":
            out = rt.restore_state(parts[2])
            self._send(201, out)
            return

        # E2B-shaped aliases. Restore / create always mint a new sandbox_id.
        if method == "POST" and parts == ["sandboxes"]:
            if body.get("state_id"):
                out = rt.restore_state(str(body["state_id"]))
                self._send(201, out)
                return
            out = rt.start_run(
                str(body.get("task_id") or body.get("templateID") or ""),
                int(body.get("seed") or 0),
                backend=str(body.get("backend") or "episode"))
            self._send(201, out)
            return

        if method == "POST" and len(parts) == 3 and parts[0] == "sandboxes":
            sid, action = parts[1], parts[2]
            if action in ("pause", "snapshots"):
                self._send(201, rt.commit_state(sid, prompt=body.get("prompt")))
                return
            if action == "fork":
                n = int(body.get("count") or body.get("n") or 1)
                out = rt.branch(sid, n)
                failed = any("error" in c for c in out["children"])
                self._send(207 if failed else 201, out)
                return

        self._send(404, {"error": "NotFound", "message": parsed.path})


class TraceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], runtime: Runtime):
        self.runtime = runtime
        super().__init__(addr, TraceHandler)


def serve(runtime: Runtime, host: str = "127.0.0.1", port: int = 0
          ) -> tuple[TraceServer, str]:
    httpd = TraceServer((host, port), runtime)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    bound = httpd.server_address[1]
    return httpd, f"http://{host}:{bound}"
