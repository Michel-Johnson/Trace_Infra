"""Immutable imports. Browsing is separate from explicitly triggered analysis."""
import copy
import hashlib
import json
import os
from pathlib import Path
from threading import RLock

from .protocol import validate
from .identity import metadata
from .timeline import view
from .ordering import source_projection
from .analysis import analyze, VERSION
from .reports import compose, validate_prices, VERSION as REPORT_VERSION
from .catalog import canonical
from .database import Repository, Conflict
from .content import LocalContentStore
from .traces import TraceRevisions


class Store:
    def __init__(self, path, prices=None, *, content_store=None):
        self.prices = validate_prices(prices)
        self.config_hash = hashlib.sha256(json.dumps(prices, sort_keys=True, allow_nan=False).encode()).hexdigest()
        self.cache_version = REPORT_VERSION + ":" + VERSION + ":" + self.config_hash
        self.repository = Repository(path)
        self.path = self.repository.path
        # Disposable SQLite stores retain their own adjacent content by default;
        # the production PostgreSQL setting must not make tests share fault fixtures.
        content_directory = (self.path.with_name(self.path.name + '.content') if self.path
                             else os.environ.get('TRACE_HUNTER_CONTENT_DIR'))
        if content_store is None and not content_directory:
            if self.path is None:
                self.repository.close()
                raise ValueError('TRACE_HUNTER_CONTENT_DIR is required for PostgreSQL; use a persistent shared directory')
        self.content = content_store if content_store is not None else LocalContentStore(content_directory)
        self.revisions = TraceRevisions(self.repository, self.content)
        self._metadata_cache = {}
        self._metadata_lock = RLock()

    def connect(self):
        return self.repository.connect()

    def close(self):
        self.repository.close()

    def import_trace(self, trace):
        validate(trace)
        payload, digest = canonical(trace)
        rid = trace['run']['id']
        result = self.revisions.append('default', payload.encode(), request_key='legacy:' + rid,
                                       derivation='legacy_import')
        created = result['created']
        return {'id': rid, 'created': created, 'digest': digest,
                'query_id': metadata(trace)['run']['query_id']}

    def list_runs(self, query_id=None):
        rows = self.repository.rows('''SELECT t.run_id AS id,r.content_digest,r.created_at AS imported_at
            FROM traces t JOIN trace_revisions r ON r.project_id=t.project_id AND r.run_id=t.run_id
             AND r.revision=t.latest_revision WHERE t.project_id='default' ORDER BY r.created_at,t.run_id''')
        result = []
        with self._metadata_lock:
            for row in rows:
                digest = row['content_digest'].removeprefix('sha256:')
                key = (row['id'], digest)
                meta = self._metadata_cache.get(key)
                if meta is None:
                    _, trace = self._read(row['id'])
                    meta = self._metadata_cache[key] = metadata(trace)
                if query_id is not None and meta['run']['query_id'] != query_id:
                    continue
                meta = copy.deepcopy(meta)
                result.append({**meta['run'], 'environment': meta['environment'],
                               'legacy_identity': meta['legacy_identity'],
                               'digest': digest, 'imported_at': row['imported_at']})
        return result

    def _read(self, rid):
        descriptor = self.revisions.get('default', rid)
        raw = self.revisions.read('default', rid, descriptor['revision'])
        return descriptor['content']['digest'].removeprefix('sha256:'), json.loads(raw)

    def get(self, rid, phases=None):
        digest, trace = self._read(rid)
        return {'trace': trace, 'digest': digest, **metadata(trace), 'view': view(source_projection(trace), phases)}

    def compare(self, ids, include_all=False):
        if not 1 <= len(ids) <= 6 or len(set(ids)) != len(ids):
            raise ValueError('请选择 1–6 个不同的运行记录')
        bundles = []
        for rid in ids:
            bundle = self.get(rid)
            if include_all:
                bundle['view'] = view(source_projection(bundle['trace']), [p['id'] for p in bundle['trace']['phases']])
            bundles.append(bundle)
        query_ids = {b['run']['query_id'] for b in bundles}
        if len(query_ids) != 1:
            raise ValueError('仅 query_id 相同的运行记录可以对比')
        return {'query_id': next(iter(query_ids)), 'runs': bundles}

    def analyze_run(self, rid, phases=None):
        """Only the explicit analysis endpoint may call this method."""
        digest, trace = self._read(rid)
        trace = source_projection(trace)
        selected = view(trace, phases)['scope']
        scope = json.dumps(sorted(set(selected)))
        result = self.repository.cached(digest, self.cache_version, scope)
        if result is None:
            result = compose(trace, analyze(trace, selected), self.prices)
            result = self.repository.save_analysis(digest, self.cache_version, scope, result)
        return {'run_id': rid, 'digest': digest, 'analysis': result}
