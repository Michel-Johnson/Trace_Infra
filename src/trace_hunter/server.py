"""Small local compatibility server for core trace import and browsing."""

import argparse
import json
import mimetypes
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

from .protocol import ROOT, SCHEMA, InvalidTrace
from .storage import Store, Conflict
from .http_contract import MAX_BYTES, phase_selection

UI = ROOT / "apps/trace-platform"


def make_handler(store, allowed_origins=()):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, value):
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers(); self.wfile.write(raw)

        def handle_error(self, error):
            status = 422 if isinstance(error, (InvalidTrace, ValueError, TypeError)) else 404 if isinstance(error, KeyError) else 500
            if isinstance(error, Conflict): status = 409
            message = "输入不符合协议" if isinstance(error, InvalidTrace) else str(error) if status != 500 else "服务器处理失败"
            self.send(status, {"error": message, "details": getattr(error, "errors", [])})

        def do_GET(self):
            try:
                url = urlparse(self.path); query = parse_qs(url.query)
                if url.path == "/api/schema": return self.send(200, SCHEMA)
                if url.path == "/api/runs": return self.send(200, store.list_runs())
                if url.path == "/api/examples":
                    rows = []
                    for path in sorted((ROOT / "examples").glob("*.trace.json")):
                        run = json.loads(path.read_text())["run"]
                        rows.append({"name": path.name, "title": " · ".join(run[k] for k in ("harness", "model", "title"))})
                    return self.send(200, rows)
                if url.path.startswith("/api/examples/"):
                    name = unquote(url.path.rsplit("/", 1)[1]); paths = {p.name: p for p in (ROOT / "examples").glob("*.trace.json")}
                    if name not in paths: raise KeyError("示例不存在")
                    return self.send(200, json.loads(paths[name].read_text()))
                if url.path.startswith("/api/runs/"):
                    return self.send(200, store.get(unquote(url.path[len("/api/runs/"):]), phase_selection(query)))
                if url.path == "/api/compare":
                    scope = query.get("scope", ["task"])[0]
                    if scope not in ("task", "all"): raise ValueError("scope 必须是 task 或 all")
                    return self.send(200, store.compare(query.get("id", []), scope == "all"))
                name = "index.html" if url.path == "/" else url.path.lstrip("/")
                if name not in ("index.html", "app.js", "theme.js", "style.css", "navigation.js"):
                    raise KeyError("页面不存在")
                raw = (UI / name).read_bytes(); self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(name)[0] + "; charset=utf-8")
                self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
            except Exception as error:
                self.handle_error(error)

        def do_POST(self):
            try:
                url = urlparse(self.path); origin = self.headers.get("Origin")
                port = self.server.server_address[1]
                if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}", *allowed_origins):
                    return self.send(403, {"error": "仅支持同源请求"})
                if url.path.startswith("/api/runs/") and url.path.endswith("/analysis"):
                    rid = unquote(url.path[len("/api/runs/"):-len("/analysis")])
                    return self.send(200, store.analyze_run(rid, phase_selection(parse_qs(url.query))))
                if url.path != "/api/import": raise KeyError("接口不存在")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BYTES: return self.send(413, {"error": "请选择不超过 16 MiB 的 JSON 文件"})
                if not self.headers.get("Content-Type", "").startswith("application/json"):
                    return self.send(415, {"error": "需要 application/json"})
                value = json.loads(self.rfile.read(size))
                if isinstance(value, dict) and value.get("schema_version") == "trace-hunter/catalog/1.0":
                    raise ValueError("核心模式不再存储评测集合；请直接导入 Trace")
                result = store.import_trace(value)
                self.send(201 if result["created"] else 200, result)
            except Exception as error:
                self.handle_error(error)
    return Handler


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--allowed-origin", action="append", default=[])
    parser.add_argument("--db", default=str(ROOT / "var/platform.sqlite")); parser.add_argument("--pricing-file")
    args = parser.parse_args(); prices = json.loads(open(args.pricing_file).read()) if args.pricing_file else None
    store = Store(args.db, prices); server = ThreadingHTTPServer((args.host, args.port), make_handler(store, args.allowed_origin))
    print(f"Trace Hunter: http://{args.host}:{server.server_port}", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); store.close()


if __name__ == "__main__":
    main()
