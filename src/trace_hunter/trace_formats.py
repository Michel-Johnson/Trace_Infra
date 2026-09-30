"""Discover the exact input profiles shipped with this release, without writes."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROFILES = {
    'v1.0': ('trace-hunter/1.0', 'stable', 'trace-v1.0.schema.json'),
    'v1.1': ('trace-hunter/1.1', 'stable', 'trace-v1.schema.json'),
    'v2-storage-1': ('trace-hunter/2.0-draft.1', 'experimental', 'trace-v2-storage-v1.schema.json'),
    'v2-storage-2': ('trace-hunter/2.0-draft.2', 'experimental', 'trace-v2-storage-v2.schema.json'),
}


def profile_schema(profile_id):
    if profile_id not in PROFILES:
        raise KeyError('Unknown trace input profile')
    return json.loads((ROOT / 'contracts/schemas' / PROFILES[profile_id][2]).read_text())


def schema_digest(schema):
    # Declared serialization, not an RFC 8785 or raw-file digest.
    raw = json.dumps(schema, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def describe_formats():
    return {
        'formats': [
            {
                'profile_id': key, 'schema_version': version, 'stability': stability,
                'schema_url': '/api/v1/trace-formats/' + key,
                'schema_digest': schema_digest(profile_schema(key)),
                'legacy_import': stability == 'stable', 'revision_import': True,
            }
            for key, (version, stability, _) in PROFILES.items()
        ],
        'schema_digest_encoding': 'sha256:sorted-json-utf8',
        'max_document_bytes': 16 * 1024 * 1024,
        'import_triggers_analysis': False,
        'note': 'Match profile_id and schema_digest; a draft version name alone does not establish compatibility. Validation also checks record references. Acceptance does not certify capture completeness, replay or training readiness.',
    }
