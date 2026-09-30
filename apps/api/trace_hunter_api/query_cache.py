"""Bounded server-side cache for read-only project queries."""

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from urllib.parse import unquote

from fastapi import Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool


CACHEABLE_SUFFIXES = frozenset((
    "traces/query", "spans/query", "spans/window", "search",
    "traces/aggregate", "objects/query", "metrics/query",
))
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheEntry:
    body: bytes
    media_type: str
    expires_at: float
    size: int


class ServerQueryCache:
    """Cache JSON responses without weakening project or revision isolation."""

    def __init__(self, repository, *, ttl_seconds=60, max_entries=256,
                 max_bytes=64 * 1024 * 1024, max_item_bytes=8 * 1024 * 1024,
                 version_ttl_ms=1000):
        self.repository = repository
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_item_bytes = max_item_bytes
        self.version_ttl_ms = version_ttl_ms
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = RLock()
        self._key_locks = {}
        self._versions = {}
        self._generations = {}

    @classmethod
    def from_environment(cls, repository):
        def integer(name, default, minimum, maximum):
            raw = os.environ.get(name)
            try:
                value = default if raw is None else int(raw)
            except ValueError:
                raise ValueError(name + " must be an integer") from None
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
            return value
        return cls(
            repository,
            ttl_seconds=integer("TRACE_HUNTER_QUERY_CACHE_TTL_SECONDS", 60, 1, 3600),
            max_entries=integer("TRACE_HUNTER_QUERY_CACHE_MAX_ENTRIES", 256, 1, 4096),
            max_bytes=integer("TRACE_HUNTER_QUERY_CACHE_MAX_BYTES", 64 * 1024 * 1024,
                              1024 * 1024, 1024 * 1024 * 1024),
            max_item_bytes=integer("TRACE_HUNTER_QUERY_CACHE_MAX_ITEM_BYTES", 8 * 1024 * 1024,
                                   1024, 64 * 1024 * 1024),
            version_ttl_ms=integer("TRACE_HUNTER_QUERY_CACHE_VERSION_TTL_MS", 1000, 0, 10000),
        )

    def data_version(self, project_id):
        """Return a cheap fingerprint that changes after source or projection writes."""
        now = time.monotonic()
        with self._lock:
            cached = self._versions.get(project_id)
            generation = self._generations.get(project_id, 0)
            if cached is not None and cached[0] > now and cached[1] == generation:
                return cached[2]
        traces = self.repository.rows(
            "SELECT COUNT(*) AS trace_count, COALESCE(SUM(latest_revision),0) AS revision_sum, "
            "COALESCE(MAX(updated_at),'') AS updated_at FROM traces WHERE project_id=:project",
            {"project": project_id},
        )[0]
        revisions = self.repository.rows(
            "SELECT COUNT(*) AS revision_count, "
            "COALESCE(SUM(CASE WHEN projection_state='complete' THEN 1 ELSE 0 END),0) AS complete_count, "
            "COALESCE(SUM(CASE WHEN projection_state='failed' THEN 1 ELSE 0 END),0) AS failed_count, "
            "COALESCE(MAX(COALESCE(indexed_at,created_at)),'') AS projection_at "
            "FROM trace_revisions WHERE project_id=:project",
            {"project": project_id},
        )[0]
        encoded = json.dumps({"generation": generation, "traces": traces, "revisions": revisions}, sort_keys=True,
                             separators=(",", ":"), default=str).encode()
        version = hashlib.sha256(encoded).hexdigest()[:24]
        with self._lock:
            if self._generations.get(project_id, 0) == generation:
                self._versions[project_id] = (
                    time.monotonic() + self.version_ttl_ms / 1000, generation, version)
        return version

    def invalidate_project(self, project_id):
        with self._lock:
            self._generations[project_id] = self._generations.get(project_id, 0) + 1
            self._versions.pop(project_id, None)

    @staticmethod
    def key(path, query, body, principal, data_version):
        identity = {
            "operator": bool(getattr(principal, "is_operator", False)),
            "project": getattr(principal, "project_id", None),
            "principal": getattr(principal, "principal_id", None),
            "scopes": sorted(getattr(principal, "scopes", ())),
        }
        digest = hashlib.sha256()
        for value in (path.encode(), query.encode(), body,
                      json.dumps(identity, sort_keys=True, separators=(",", ":")).encode(),
                      data_version.encode()):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
        return digest.hexdigest()

    def get(self, key):
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._remove(key)
                return None
            self._entries.move_to_end(key)
            return entry

    def put(self, key, body, media_type):
        size = len(body)
        if size > self.max_item_bytes or size > self.max_bytes:
            return False
        entry = CacheEntry(body=body, media_type=media_type,
                           expires_at=time.monotonic() + self.ttl_seconds, size=size)
        with self._lock:
            self._remove(key)
            self._entries[key] = entry
            self._bytes += size
            while len(self._entries) > self.max_entries or self._bytes > self.max_bytes:
                self._remove(next(iter(self._entries)))
        return True

    def _remove(self, key):
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._bytes -= entry.size

    def lock_for(self, key):
        with self._lock:
            lease = self._key_locks.get(key)
            if lease is None:
                lock = asyncio.Lock()
                self._key_locks[key] = [lock, 1]
            else:
                lock = lease[0]
                lease[1] += 1
            return lock

    def release_lock(self, key, lock):
        with self._lock:
            lease = self._key_locks.get(key)
            if lease is None or lease[0] is not lock:
                return
            lease[1] -= 1
            if lease[1] == 0:
                self._key_locks.pop(key, None)

    def status(self):
        with self._lock:
            return {"entries": len(self._entries), "bytes": self._bytes,
                    "ttl_seconds": self.ttl_seconds, "max_entries": self.max_entries,
                    "max_bytes": self.max_bytes, "max_item_bytes": self.max_item_bytes,
                    "version_ttl_ms": self.version_ttl_ms}


def project_target(request):
    prefix = "/api/v1/projects/"
    path = request.url.path
    if not path.startswith(prefix):
        return None
    project, separator, _ = path[len(prefix):].partition("/")
    return unquote(project) if separator else None


def cache_target(request):
    if request.method != "POST":
        return None
    project = project_target(request)
    if project is None:
        return None
    _, _, suffix = request.url.path[len("/api/v1/projects/"):].partition("/")
    if suffix not in CACHEABLE_SUFFIXES:
        return None
    return project


def install(app, cache):
    app.state.query_cache = cache

    async def observe_evaluation_search(request, body, response_body, project_id, started_at):
        if not request.url.path.endswith("/search"):
            return
        try:
            query = json.loads(body)
            if (not isinstance(query, dict) or query.get("purpose") != "evaluation" or
                    query.get("scope") != "analysis" or
                    query.get("mode", "literal") != "literal" or
                    query.get("cursor") is not None or
                    not isinstance(query.get("query"), str) or
                    not 2 <= len(query["query"]) <= 128):
                return
            total_count = json.loads(response_body)["total_count"]
            await run_in_threadpool(app.state.trace_search.hot_terms.observe,
                                    project_id, query["query"],
                                    (time.monotonic() - started_at) * 1000, total_count)
        except (ValueError, TypeError, KeyError):
            return
        except Exception:
            LOG.warning("Evaluation search statistics could not be recorded")

    @app.middleware("http")
    async def server_query_cache(request: Request, call_next):
        started_at = time.monotonic()
        project_id = cache_target(request)
        principal = getattr(request.state, "service_principal", None)
        if project_id is None or principal is None:
            response = await call_next(request)
            changed_project = project_target(request)
            if (principal is not None and changed_project is not None and
                    request.method in ("POST", "PUT", "PATCH", "DELETE") and
                    response.status_code < 400):
                cache.invalidate_project(changed_project)
            return response
        body = await request.body()
        version = await run_in_threadpool(cache.data_version, project_id)
        key = cache.key(request.url.path, request.url.query, body, principal, version)
        bypass = "no-cache" in request.headers.get("cache-control", "").lower()
        if not bypass:
            cached = cache.get(key)
            if cached is not None:
                await observe_evaluation_search(request, body, cached.body, project_id, started_at)
                return Response(cached.body, media_type=cached.media_type, headers={
                    "Cache-Control": "no-store", "X-Trace-Hunter-Cache": "HIT",
                    "X-Trace-Hunter-Data-Version": version,
                })
        lock = cache.lock_for(key)
        try:
            async with lock:
                if not bypass:
                    cached = cache.get(key)
                    if cached is not None:
                        await observe_evaluation_search(request, body, cached.body, project_id, started_at)
                        return Response(cached.body, media_type=cached.media_type, headers={
                            "Cache-Control": "no-store", "X-Trace-Hunter-Cache": "HIT",
                            "X-Trace-Hunter-Data-Version": version,
                        })
                response = await call_next(request)
                response_body = b"".join([chunk async for chunk in response.body_iterator])
                if response.status_code == 200:
                    await observe_evaluation_search(request, body, response_body, project_id, started_at)
                headers = dict(response.headers)
                media_type = headers.get("content-type", "application/json").split(";", 1)[0]
                stored = response.status_code == 200 and media_type == "application/json" and cache.put(
                    key, response_body, media_type)
                headers["X-Trace-Hunter-Cache"] = "BYPASS" if bypass else "MISS"
                headers["X-Trace-Hunter-Data-Version"] = version
                if not stored and not bypass and response.status_code == 200:
                    headers["X-Trace-Hunter-Cache"] = "SKIP"
                return Response(response_body, status_code=response.status_code,
                                media_type=media_type, headers=headers)
        finally:
            cache.release_lock(key, lock)
