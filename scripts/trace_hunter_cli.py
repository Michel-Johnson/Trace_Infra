#!/usr/bin/env python3
"""JSON-first command line client for the Trace Hunter Core API."""

import argparse
import base64
import http.cookiejar
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import UUID
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, HTTPCookieProcessor


class CLIError(Exception):
    def __init__(self, message, *, code=5, status=None, details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or []


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise CLIError(message, code=2)


def _config_path(environ):
    configured = environ.get("TRACE_HUNTER_CONFIG")
    return Path(configured).expanduser() if configured else Path.home() / ".config/trace-hunter/config.json"


def _load_config(environ):
    path = _config_path(environ)
    if not path.exists():
        return path, {"version": 1, "current": "local", "profiles": {}}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise CLIError("Invalid CLI profile config: " + str(error)) from None
    if (not isinstance(value, dict) or value.get("version") != 1 or
            not isinstance(value.get("profiles", {}), dict)):
        raise CLIError("CLI profile config must use version 1")
    value.setdefault("current", "local"); value.setdefault("profiles", {})
    return path, value


def _write_config(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending = path.with_name("." + path.name + ".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    os.chmod(pending, 0o600); pending.replace(path)


def _profile(args, environ):
    path, config = _load_config(environ)
    name = args.profile or environ.get("TRACE_HUNTER_PROFILE") or config.get("current") or "local"
    profiles = {**config["profiles"], "local": {"url": "http://127.0.0.1:8766"}}
    if name not in profiles:
        raise CLIError("Unknown Trace Hunter profile: " + name)
    profile = profiles[name]
    if not isinstance(profile, dict) or not isinstance(profile.get("url"), str):
        raise CLIError("Trace Hunter profile requires a URL")
    effective = dict(environ)
    for field, env_name in (("project", "TRACE_HUNTER_PROJECT"), ("username", "TRACE_HUNTER_USERNAME"),
                            ("timeout", "TRACE_HUNTER_TIMEOUT")):
        if field in profile and env_name not in effective:
            effective[env_name] = str(profile[field])
    return name, profile, effective


def _profile_command(args, environ):
    path, config = _load_config(environ)
    profiles = {**config["profiles"], "local": {"url": "http://127.0.0.1:8766"}}
    if args.action == "list":
        return {"current": config.get("current", "local"), "config": str(path),
                "profiles": [{"name": name, **value} for name, value in sorted(profiles.items())]}
    if args.action == "get":
        if args.name not in profiles: raise CLIError("Unknown Trace Hunter profile: " + args.name)
        return {"name": args.name, **profiles[args.name]}
    if args.action == "set":
        if args.name == "local": raise CLIError("The built-in local profile cannot be replaced")
        if not args.url.startswith(("http://", "https://")): raise CLIError("Profile URL must use http or https")
        value = {"url": args.url.rstrip("/")}
        for field in ("project", "username", "timeout"):
            item = getattr(args, field)
            if item is not None: value[field] = item
        config["profiles"][args.name] = value; _write_config(path, config)
        return {"name": args.name, **value, "config": str(path)}
    if args.name not in profiles: raise CLIError("Unknown Trace Hunter profile: " + args.name)
    config["current"] = args.name; _write_config(path, config)
    return {"current": args.name, "config": str(path)}


def _json_object(value):
    if value is None:
        return {}
    try:
        raw = Path(value[1:]).read_text() if value.startswith("@") else value
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise CLIError("Invalid JSON body: " + str(error)) from None
    if not isinstance(parsed, dict):
        raise CLIError("JSON body must be an object")
    return parsed


def _csv(value):
    return [item for part in value or [] for item in part.split(",") if item]


def _project(args, environ):
    value = getattr(args, "project", None) or environ.get("TRACE_HUNTER_PROJECT")
    if not value:
        raise CLIError("Set --project or TRACE_HUNTER_PROJECT")
    return quote(value, safe="")


class Client:
    def __init__(self, url, environ):
        self.url = url.rstrip("/")
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.headers = {"Accept": "application/json"}
        try:
            self.timeout = float(environ.get("TRACE_HUNTER_TIMEOUT", "60"))
        except ValueError:
            raise CLIError("TRACE_HUNTER_TIMEOUT must be numeric") from None
        if not 1 <= self.timeout <= 3600:
            raise CLIError("TRACE_HUNTER_TIMEOUT must be between 1 and 3600 seconds")
        token = environ.get("TRACE_HUNTER_TOKEN")
        username, password = environ.get("TRACE_HUNTER_USERNAME"), environ.get("TRACE_HUNTER_PASSWORD")
        if token:
            self.headers["Authorization"] = "Bearer " + token
        elif username is not None or password is not None:
            if username is None or password is None:
                raise CLIError("Set both TRACE_HUNTER_USERNAME and TRACE_HUNTER_PASSWORD")
            encoded = base64.b64encode((username + ":" + password).encode()).decode()
            self.headers["Authorization"] = "Basic " + encoded

    def request(self, method, path, *, body=None, raw=None, headers=None):
        request_headers = {**self.headers, **(headers or {})}
        data = raw
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            request_headers["Content-Type"] = "application/json"
        request = Request(self.url + path, data=data, headers=request_headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = response.read()
                content_type = response.headers.get("content-type", "")
                return json.loads(payload) if "json" in content_type or payload[:1] in (b"{", b"[") else payload.decode()
        except HTTPError as error:
            raw_error = error.read()
            try:
                payload = json.loads(raw_error)
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {"error": "HTTP request failed", "details": []}
            raise CLIError(payload.get("error", "HTTP request failed"),
                           code=3 if error.code < 500 else 4, status=error.code,
                           details=payload.get("details", [])) from None
        except URLError as error:
            raise CLIError("Trace Hunter is unavailable: " + str(error.reason), code=4) from None

    def events(self, path, *, after=0):
        headers = {**self.headers, "Accept": "text/event-stream"}
        if after:
            headers["Last-Event-ID"] = str(after)
        request = Request(self.url + path, headers=headers, method="GET")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                data = []
                for raw_line in response:
                    line = raw_line.decode("utf-8").rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        yield json.loads("\n".join(data)); data = []
                if data:
                    yield json.loads("\n".join(data))
        except HTTPError as error:
            raise CLIError("Task event stream failed", code=3 if error.code < 500 else 4,
                           status=error.code) from None
        except (URLError, TimeoutError) as error:
            raise CLIError("Task event stream unavailable: " + str(error), code=4) from None


def _collect(client, path, body, all_pages):
    first = client.request("POST", path, body=body)
    if not all_pages or not first.get("next_cursor"):
        return first
    items = list(first.get("items", [])); cursor = first.get("next_cursor")
    while cursor:
        page = client.request("POST", path, body={**body, "cursor": cursor})
        items.extend(page.get("items", [])); cursor = page.get("next_cursor")
    return {**first, "items": items, "next_cursor": None, "pages_collected": True}


def _query_body(args):
    body = _json_object(args.body)
    if getattr(args, "limit", None) is not None: body["limit"] = args.limit
    if getattr(args, "cursor", None): body["cursor"] = args.cursor
    if getattr(args, "revisions", None): body["revisions"] = args.revisions
    if getattr(args, "order", None): body["order"] = args.order
    return body


def execute(args, client, environ):
    project = lambda: _project(args, environ)
    if args.command == "capabilities":
        paths = {"trace": "/api/v1/query-capabilities", "span": "/api/v1/span-query-capabilities",
                 "search": "/api/v1/trace-search-capabilities",
                 "advanced": "/api/v1/advanced-query-capabilities",
                 "import": "/api/v1/adapter-import-capabilities",
                 "task": "/api/v1/task-capabilities"}
        names = paths if args.kind == "all" else (args.kind,)
        return {name: client.request("GET", paths[name]) for name in names}
    if args.command == "project":
        if args.action == "list":
            return client.request("GET", "/api/v1/projects?" + urlencode({"limit": args.limit}))
        if args.action == "create":
            return client.request("POST", "/api/v1/projects", body={"project_id": args.project_id, "name": args.name})
        return client.request("GET", "/api/v1/projects/" + quote(args.project_id, safe=""))
    if args.command == "import":
        try: raw = Path(args.file).read_bytes()
        except OSError as error: raise CLIError("Cannot read trace: " + str(error)) from None
        key = args.idempotency_key or "cli:" + hashlib.sha256(raw).hexdigest()
        query = urlencode({"expected_previous": args.expected_previous, "derivation": args.derivation})
        return client.request("POST", f"/api/v1/projects/{project()}/traces?{query}", raw=raw,
                              headers={"Content-Type": "application/json", "Idempotency-Key": key})
    if args.command == "task":
        base = f"/api/v1/projects/{project()}/tasks"
        if args.action == "list":
            params = [("limit", args.limit)] + [("state", item) for item in args.state] + [("kind", item) for item in args.kind]
            if args.cursor: params.append(("after", args.cursor))
            return client.request("GET", base + "?" + urlencode(params))
        if args.action == "create":
            body = _json_object(args.body)
            body.update(kind=args.kind, title=args.title,
                        request_key=args.request_key or "cli-task:" + hashlib.sha256((args.kind + "\0" + args.title).encode()).hexdigest())
            native_session = (environ.get("MULMOTERMINAL_SESSION_ID") if args.profile == "terminal"
                              else environ.get("TRACE_HUNTER_AGENT_SESSION_ID") if args.profile == "agent" else None)
            if native_session:
                try:
                    body["agent_session_id"] = str(UUID(native_session))
                except ValueError:
                    raise CLIError("Invalid MulmoTerminal session identity") from None
            return client.request("POST", base, body=body)
        path = base + "/" + quote(args.task_id, safe="")
        if args.action == "get": return client.request("GET", path)
        if args.action == "update": return client.request("PATCH", path, body=_json_object(args.body))
        if args.action == "cancel": return client.request("POST", path + "/cancel", body={})
        if args.action == "retry": return client.request("POST", path + "/retry", body={})
    if args.command == "trace":
        run = quote(getattr(args, "run_id", ""), safe="")
        if args.action == "query":
            return _collect(client, f"/api/v1/projects/{project()}/traces/query",
                            _query_body(args), args.all_pages)
        if args.action == "aggregate":
            return client.request("POST", f"/api/v1/projects/{project()}/traces/aggregate",
                                  body=_query_body(args))
        base = f"/api/v1/projects/{project()}/traces/{run}/revisions"
        if args.action == "history":
            params = {"limit": args.limit}
            if args.before is not None: params["before"] = args.before
            return client.request("GET", base + "?" + urlencode(params))
        path = f"{base}/{args.revision}"
        if args.action == "get": return client.request("GET", path)
        if args.action == "content": return client.request("GET", path + "/content")
        return client.request("POST", path + "/index", body={})
    if args.command == "span":
        body = _query_body(args)
        filters = body.setdefault("filters", {})
        for name in ("skill_name", "skill_action", "run_id", "name", "operation", "status"):
            values = _csv(getattr(args, name, None))
            if values: filters[name] = values
        if args.min_duration_ms is not None: filters["min_duration_ms"] = args.min_duration_ms
        if args.max_duration_ms is not None: filters["max_duration_ms"] = args.max_duration_ms
        if args.order: body["order"] = args.order
        if args.fields: body["fields"] = _csv(args.fields)
        return _collect(client, f"/api/v1/projects/{project()}/spans/query", body, args.all_pages)
    if args.command == "span-window":
        body = _json_object(args.body)
        body.update(anchor={"run_id": args.run_id, "revision": args.revision,
                            "span_id": args.span_id}, before=args.before, after=args.after,
                    preview_chars=args.preview_chars)
        if args.fields: body["fields"] = _csv(args.fields)
        if args.include: body["include"] = _csv(args.include)
        return client.request("POST", f"/api/v1/projects/{project()}/spans/window", body=body)
    if args.command == "search":
        if args.evaluation and (args.scope != "analysis" or args.mode != "literal"):
            raise CLIError("--evaluation requires analysis scope and literal mode")
        if args.regex_syntax and args.mode != "regex":
            raise CLIError("--regex-syntax requires --mode regex")
        body = _query_body(args)
        body.update(query=args.query, mode=args.mode, scope=args.scope)
        if args.regex_syntax: body["regex_syntax"] = args.regex_syntax
        if args.evaluation: body["purpose"] = "evaluation"
        filters = body.setdefault("filters", {})
        for name in ("run_id", "span_id", "field", "object_kind"):
            values = _csv(getattr(args, name))
            if values: filters[name] = values
        return _collect(client, f"/api/v1/projects/{project()}/search", body, args.all_pages)
    if args.command == "search-terms":
        return client.request("GET", f"/api/v1/projects/{project()}/search/terms")
    if args.command in ("search-term-promote", "search-term-demote"):
        action = "promote" if args.command.endswith("promote") else "demote"
        return client.request("POST", f"/api/v1/projects/{project()}/search/terms/{action}",
                              body={"term": args.term})
    if args.command == "object-query":
        return _collect(client, f"/api/v1/projects/{project()}/objects/query",
                        _query_body(args), args.all_pages)
    if args.command == "metrics-query":
        return client.request("POST", f"/api/v1/projects/{project()}/metrics/query",
                              body=_json_object(args.body))
    if args.command == "evidence-export":
        return client.request("POST", f"/api/v1/projects/{project()}/evidence-exports",
                              body=_json_object(args.body))
    if args.command in ("upload", "adapter-import"):
        if args.command == "adapter-import" and args.source_format == "auto":
            raise CLIError("adapter-import requires a registered format; use upload for Agent import")
        if (args.batch_id is None) != (args.batch_total is None):
            raise CLIError("Set --batch-id and --batch-total together")
        path = Path(args.file)
        try:
            size = path.stat().st_size
            hasher = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
        except OSError as error:
            raise CLIError("Cannot read trace: " + str(error)) from None
        source_name = args.source_name if args.command == "adapter-import" else args.source_name or path.name
        default_key = ("adapter-cli:" + digest if args.command == "adapter-import" else
                       "upload-cli:" + digest + ":" + hashlib.sha256(source_name.encode()).hexdigest())
        request_key = args.idempotency_key or default_key
        if args.source_format == "auto":
            capabilities = client.request("GET", "/api/v1/agent/capabilities")
            if not capabilities.get("enabled"):
                raise CLIError("Automatic Agent import is not enabled on this server")
        body = {"request_key": request_key, "source_format": args.source_format,
                "size_bytes": size, "sha256": digest, "part_size": args.part_size,
                "binding": {"run_id": args.run_id, "query_id": args.query_id, "env_id": args.env_id},
                "expected_previous": args.expected_previous, "source_name": source_name}
        if args.batch_id is not None:
            body.update(batch_id=args.batch_id, batch_total=args.batch_total)
        root = f"/api/v1/projects/{project()}/imports/uploads"
        status = client.request("POST", root, body=body)
        with path.open("rb") as source:
            for position in status["missing_parts"]:
                source.seek(position * status["part_size"])
                chunk = source.read(status["part_size"])
                status = client.request("PUT", root + "/" + quote(status["upload_id"], safe="") +
                                        "/parts/" + str(position), raw=chunk,
                                        headers={"Content-Type": "application/octet-stream",
                                                 "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()})
        return client.request("POST", root + "/" + quote(status["upload_id"], safe="") + "/complete", body={})
    if args.command == "import-adapters":
        return client.request("GET", f"/api/v1/projects/{project()}/import-adapters")
    if args.command == "import-adapter-download":
        if len(args.sha256) != 64 or any(ch not in "0123456789abcdef" for ch in args.sha256):
            raise CLIError("Adapter SHA-256 must be 64 lowercase hex characters", code=2)
        url = client.url + f"/api/v1/projects/{project()}/import-adapters/{args.sha256}/content"
        try:
            with client.opener.open(Request(url, headers=client.headers), timeout=client.timeout) as source:
                content = source.read(256 * 1024 + 1)
        except (HTTPError, URLError, TimeoutError) as error:
            raise CLIError("Cannot download Adapter: " + str(error), code=4) from None
        if len(content) > 256 * 1024 or hashlib.sha256(content).hexdigest() != args.sha256:
            raise CLIError("Downloaded Adapter digest or size mismatch", code=4)
        destination = Path(args.output)
        try:
            with destination.open("xb") as output:
                output.write(content)
        except OSError as error:
            raise CLIError("Cannot save Adapter: " + str(error)) from None
        return {"path": str(destination), "sha256": args.sha256, "size_bytes": len(content)}
    raise CLIError("Unsupported command")


def parser():
    root = JsonArgumentParser(prog="trace-hunter", description="Trace Hunter Core API CLI")
    root.add_argument("--url", default=None, help="API origin; defaults to TRACE_HUNTER_URL")
    root.add_argument("--profile", help="Connection profile; defaults to TRACE_HUNTER_PROFILE or current profile")
    root.add_argument("--project", help="Project; defaults to TRACE_HUNTER_PROJECT")
    root.add_argument("--compact", action="store_true", help="Emit compact JSON")
    commands = root.add_subparsers(dest="command", required=True)

    profiles = commands.add_parser("profile").add_subparsers(dest="action", required=True)
    profiles.add_parser("list")
    profile_get = profiles.add_parser("get"); profile_get.add_argument("name")
    profile_use = profiles.add_parser("use"); profile_use.add_argument("name")
    profile_set = profiles.add_parser("set"); profile_set.add_argument("name"); profile_set.add_argument("--url", required=True)
    profile_set.add_argument("--project"); profile_set.add_argument("--username"); profile_set.add_argument("--timeout", type=float)

    capability = commands.add_parser("capabilities")
    capability.add_argument("kind", choices=("all", "trace", "span", "search", "advanced"), nargs="?", default="all")

    projects = commands.add_parser("project").add_subparsers(dest="action", required=True)
    listing = projects.add_parser("list"); listing.add_argument("--limit", type=int, default=50)
    create = projects.add_parser("create"); create.add_argument("project_id"); create.add_argument("name")
    get = projects.add_parser("get"); get.add_argument("project_id")

    importing = commands.add_parser("import")
    importing.add_argument("file"); importing.add_argument("--idempotency-key")
    importing.add_argument("--expected-previous", type=int, default=0)
    importing.add_argument("--derivation", choices=("capture", "supplement", "correction"), default="capture")

    adapter = commands.add_parser("adapter-import")
    adapter.add_argument("--part-size", type=int, default=8 * 1024 * 1024)
    adapter.add_argument("file"); adapter.add_argument("--source-format", required=True)
    adapter.add_argument("--idempotency-key"); adapter.add_argument("--expected-previous", type=int, default=0)
    adapter.add_argument("--run-id"); adapter.add_argument("--query-id"); adapter.add_argument("--env-id")
    adapter.add_argument("--batch-id"); adapter.add_argument("--batch-total", type=int); adapter.add_argument("--source-name")
    adapter.add_argument("--wait", action="store_true", help="Stream task snapshots until completion")

    tasks = commands.add_parser("task").add_subparsers(dest="action", required=True)
    task_list = tasks.add_parser("list"); task_list.add_argument("--state", action="append", default=[])
    task_list.add_argument("--kind", action="append", default=[]); task_list.add_argument("--limit", type=int, default=100)
    task_list.add_argument("--cursor")
    task_create = tasks.add_parser("create"); task_create.add_argument("kind", choices=("evaluation", "analysis", "custom"))
    task_create.add_argument("title"); task_create.add_argument("--request-key"); task_create.add_argument("--body")
    for action in ("get", "watch", "cancel", "retry"):
        command = tasks.add_parser(action); command.add_argument("task_id")
    task_update = tasks.add_parser("update"); task_update.add_argument("task_id"); task_update.add_argument("--body", required=True)
    tasks.choices["watch"].add_argument("--after", type=int, default=0)

    traces = commands.add_parser("trace").add_subparsers(dest="action", required=True)
    query = traces.add_parser("query"); _query_arguments(query, all_pages=True)
    query.add_argument("--order", choices=("run_id_asc_revision_asc", "created_at_desc"))
    aggregate = traces.add_parser("aggregate"); _query_arguments(aggregate)
    history = traces.add_parser("history"); history.add_argument("run_id"); history.add_argument("--before", type=int); history.add_argument("--limit", type=int, default=50)
    for action in ("get", "content", "index"):
        item = traces.add_parser(action); item.add_argument("run_id"); item.add_argument("revision", type=int)

    span = commands.add_parser("span"); _query_arguments(span, all_pages=True)
    for name in ("skill-name", "skill-action", "run-id", "name", "operation", "status", "fields"):
        span.add_argument("--" + name, action="append")
    span.add_argument("--min-duration-ms", type=float); span.add_argument("--max-duration-ms", type=float)
    span.add_argument("--order", choices=("source", "duration_desc"))

    window = commands.add_parser("span-window")
    window.add_argument("run_id"); window.add_argument("revision", type=int); window.add_argument("span_id")
    window.add_argument("--before", type=int, default=20); window.add_argument("--after", type=int, default=20)
    window.add_argument("--fields", action="append"); window.add_argument("--include", action="append")
    window.add_argument("--preview-chars", type=int, default=512); window.add_argument("--body")

    search = commands.add_parser("search"); search.add_argument("query")
    search.add_argument("--mode", choices=("literal", "regex"), default="literal")
    search.add_argument("--regex-syntax", choices=("postgresql_are", "portable"),
                        help="PostgreSQL ARE (default), or portable \\b/\\B word-boundary aliases")
    search.add_argument("--scope", choices=("analysis", "model_context"), default="analysis")
    search.add_argument("--evaluation", action="store_true",
                        help="Record this analysis-scope literal query for the evaluation term vocabulary")
    for name in ("run-id", "span-id", "field", "object-kind"):
        search.add_argument("--" + name, action="append", default=[])
    _query_arguments(search, all_pages=True)
    commands.add_parser("search-terms")
    for name in ("search-term-promote", "search-term-demote"):
        command = commands.add_parser(name); command.add_argument("term")

    object_query = commands.add_parser("object-query"); _query_arguments(object_query, all_pages=True)
    metrics = commands.add_parser("metrics-query"); metrics.add_argument("--body", required=True)
    evidence = commands.add_parser("evidence-export"); evidence.add_argument("--body", required=True)
    upload = commands.add_parser("upload"); upload.add_argument("file"); upload.add_argument("--source-format", default="auto")
    upload.add_argument("--idempotency-key"); upload.add_argument("--part-size", type=int, default=8 * 1024 * 1024)
    upload.add_argument("--expected-previous", type=int, default=0)
    upload.add_argument("--run-id"); upload.add_argument("--query-id"); upload.add_argument("--env-id")
    upload.add_argument("--batch-id"); upload.add_argument("--batch-total", type=int); upload.add_argument("--source-name")
    upload.add_argument("--wait", action="store_true", help="Stream adapter task snapshots until completion")
    commands.add_parser("import-adapters")
    adapter_download = commands.add_parser("import-adapter-download")
    adapter_download.add_argument("sha256"); adapter_download.add_argument("output")
    return root


def _query_arguments(command, all_pages=False):
    command.add_argument("--body", help="JSON object or @path")
    command.add_argument("--revisions", choices=("latest", "all"))
    command.add_argument("--limit", type=int); command.add_argument("--cursor")
    if all_pages: command.add_argument("--all-pages", action="store_true")


def main(argv=None, *, environ=None, stdout=None, stderr=None):
    environ = os.environ if environ is None else environ
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    try:
        args = parser().parse_args(argv)
        if args.command == "profile":
            result = _profile_command(args, environ)
            json.dump(result, stdout, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":") if args.compact else None, indent=None if args.compact else 2)
            stdout.write("\n")
            return 0
        profile_name, profile, effective_environ = _profile(args, environ)
        args.profile = profile_name
        url = args.url or environ.get("TRACE_HUNTER_URL") or profile["url"]
        client = Client(url, effective_environ)
        if args.command == "task" and args.action == "watch":
            project = _project(args, effective_environ)
            path = f"/api/v1/projects/{project}/tasks/{quote(args.task_id, safe='')}/events"
            for result in client.events(path, after=args.after):
                json.dump(result, stdout, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":") if args.compact else None)
                stdout.write("\n"); stdout.flush()
            return 0
        result = execute(args, client, effective_environ)
        if args.command in ("adapter-import", "upload") and args.wait:
            json.dump(result, stdout, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":") if args.compact else None)
            stdout.write("\n"); stdout.flush()
            project = _project(args, effective_environ)
            task_id = result.get("task_id") or result.get("job_id")
            if not task_id: raise CLIError("Import did not return a task ID", code=4)
            path = f"/api/v1/projects/{project}/tasks/{quote(task_id, safe='')}/events"
            final = None
            for update in client.events(path, after=int(result.get("revision", 0))):
                json.dump(update, stdout, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":") if args.compact else None)
                stdout.write("\n"); stdout.flush()
                if update.get("state") in {"succeeded", "failed", "cancelled"}:
                    final = update
            if final and final.get("state") != "succeeded":
                task_error = final.get("error") if isinstance(final.get("error"), dict) else {}
                raise CLIError(task_error.get("message") or "Adapter import task " + final["state"],
                               code=4, details=task_error.get("issues") or [])
            return 0
        json.dump(result, stdout, ensure_ascii=False, allow_nan=False,
                  separators=(",", ":") if args.compact else None, indent=None if args.compact else 2)
        stdout.write("\n")
        return 0
    except CLIError as error:
        json.dump({"error": str(error), "status": error.status, "details": error.details}, stderr,
                  ensure_ascii=False, allow_nan=False)
        stderr.write("\n")
        return error.code


if __name__ == "__main__":
    raise SystemExit(main())
