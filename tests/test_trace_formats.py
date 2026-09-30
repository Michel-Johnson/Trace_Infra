"""Discovery stays authenticated, read-only and tied to the accepted schema."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'apps/api')]
from fastapi.testclient import TestClient
from trace_hunter.storage import Store
from trace_hunter.trace_formats import describe_formats, profile_schema, schema_digest
from trace_hunter.traces.formats import STORAGE_V2_SCHEMA, STORAGE_V2_SCHEMAS
from trace_hunter_api.app import create_app
from scripts.build_trace_v2_storage_v2 import build as build_v2_storage


class TraceFormatsTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {'TRACE_HUNTER_REQUIRE_SERVICE_AUTH': 'false', 'TRACE_HUNTER_OPERATOR_NETWORKS': '127.0.0.1/32'}))
        self.store = Store(Path(self.directory) / 'formats.sqlite')
        self.addCleanup(self.store.close)
        self.app = create_app(self.store)
        self.client = self.enterContext(TestClient(self.app, client=('127.0.0.1', 50000)))

    def test_profiles_match_shipped_schemas_without_analyzing(self):
        response = self.client.get('/api/v1/trace-formats')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['import_triggers_analysis'])
        self.assertEqual(profile_schema('v2-storage-1'), STORAGE_V2_SCHEMA)
        self.assertEqual(profile_schema('v2-storage-2'), STORAGE_V2_SCHEMAS['trace-hunter/2.0-draft.2'])
        self.assertEqual(profile_schema('v2-storage-2'), build_v2_storage())
        for item in response.json()['formats']:
            schema = self.client.get(item['schema_url']).json()
            self.assertEqual(schema_digest(schema), item['schema_digest'])
            self.assertEqual(schema, profile_schema(item['profile_id']))
        for table in ('traces', 'trace_revisions', 'trace_objects', 'trace_edges',
                      'trace_search_documents', 'analysis_results'):
            self.assertEqual(self.store.repository.rows('select count(*) as n from ' + table)[0]['n'], 0)
        self.assertEqual(self.client.get('/api/v1/trace-formats/missing').status_code, 404)

    def test_discovery_requires_a_trusted_network_peer(self):
        with TestClient(self.app, client=('203.0.113.8', 50000)) as remote:
            self.assertEqual(remote.get('/api/v1/trace-formats').status_code, 403)
        self.assertEqual(self.client.get('/api/v1/trace-formats').status_code, 200)
