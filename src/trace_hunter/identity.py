"""Import identity and environment metadata, independent of later analysis."""
from hashlib import sha256


def legacy_env_id(harness):
    known = {'Doubao Work': 'legacy-doubao-work', 'Claude Code': 'legacy-claude-code'}
    return known.get(harness, 'legacy-' + sha256(harness.encode()).hexdigest()[:12])


def unknown_environment():
    return {'isolation': 'unknown', 'network_access': 'unknown', 'observed_at': None,
            'snapshot_id': None, 'tool_versions': {}, 'notes': ''}


def metadata(trace):
    """Compatibility is a read projection: never rewrite an old imported payload."""
    run = dict(trace['run'])
    legacy = trace['schema_version'] == 'trace-hunter/1.0'
    if legacy:
        run['query_id'] = run.pop('task_key')
        run['env_id'] = legacy_env_id(run['harness'])
    environment = {**unknown_environment(), **trace.get('environment', {})}
    return {'run': run, 'environment': environment, 'legacy_identity': legacy}


def upgrade_header(trace, query_id=None, env_id=None):
    """Used only by export adapters, not by the immutable import store."""
    meta = metadata(trace)
    trace['run'] = meta['run']
    trace['environment'] = meta['environment']
    trace['schema_version'] = 'trace-hunter/1.1'
    if query_id is not None:
        trace['run']['query_id'] = query_id
    if env_id is not None:
        trace['run']['env_id'] = env_id
    return trace
