#!/usr/bin/env python3
"""Read historical inputs without importing them; freeze a body-free regression manifest."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from trace_hunter.protocol import InvalidTrace, validate
from trace_hunter.catalog import validate_catalog

VERSION = 'backend-corpus/1'
MAX_JSON_BYTES = 16 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def nonfinite(_):
        raise ValueError('nonfinite JSON number')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)


def relative(name):
    value = PurePosixPath(name)
    if not name or value.is_absolute() or '..' in value.parts or '\\' in name:
        raise ValueError('Input paths must be relative and cannot traverse directories')
    return str(value)


def file_records(source):
    """One sequential pass, including archives; no extraction or source execution."""
    if source.is_dir():
        for directory, dirs, files in os.walk(source, followlinks=False):
            for name in sorted(dirs + files):
                if (Path(directory) / name).is_symlink():
                    raise ValueError('Input cannot contain symbolic links')
            dirs.sort()
            for name in sorted(files):
                path = Path(directory) / name
                if not path.is_file():
                    raise ValueError('Input must contain regular files')
                with path.open('rb') as stream:
                    yield path.relative_to(source).as_posix(), path.stat().st_size, stream
    else:
        with tarfile.open(source, 'r|*') as archive:
            for member in archive:
                name = relative(member.name)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError('Archive must contain regular files and directories only')
                with archive.extractfile(member) as stream:
                    yield name, member.size, stream


def trace_metadata(trace, name, sha, size):
    tools = [s for s in trace['spans'] if s['kind'] == 'tool']
    models = [s for s in trace['spans'] if s['kind'] == 'model']
    run = trace['run']
    environment = trace.get('environment', {})
    intervals = sum(s['start_ms'] is not None and s['end_ms'] is not None for s in tools)
    known = sum(s['duration_ms'] is not None or (s['start_ms'] is not None and s['end_ms'] is not None) for s in tools)
    durations = [s['duration_ms'] if s['duration_ms'] is not None else s['end_ms'] - s['start_ms']
                 for s in tools if s['duration_ms'] is not None or (s['start_ms'] is not None and s['end_ms'] is not None)]
    fragment_count = (sum(s['id'].startswith('fragment-') for s in trace['sources'])
                      if trace['collector']['name'] == 'doubao-command-corpus-adapter' else None)
    return {'path': name, 'sha256': sha, 'bytes': size, 'trace_digest': digest(canonical(trace)),
            'schema_version': trace['schema_version'], 'run_id': run['id'],
            'query_id': run.get('query_id', run.get('task_key')), 'env_id': run.get('env_id'),
            'collector_version': trace['collector']['version'],
            'isolation': environment.get('isolation', 'unknown'),
            'network_access': environment.get('network_access', 'unknown'),
            'record_count': len(trace['spans']), 'tool_count': len(tools),
            'model_record_count': len(models), 'model_usage_record_count': sum(s['usage'] is not None for s in models),
            'phase_count': len(trace['phases']), 'evidence_count': len(trace['evidence']),
            'tool_status': dict(sorted(Counter(s['status'] for s in tools).items())),
            'operations': dict(sorted(Counter(s['operation'] for s in tools).items())),
            'tool_time_known': known, 'tool_interval_known': intervals,
            'observed_tool_ms': sum(durations) if durations else None,
            'sequence_known': sum('sequence' in s for s in tools),
            'source_fragment_count': fragment_count, 'coverage': trace['coverage'],
            'source_refs': [{'id': s['id'], 'name': s['name'], 'sha256': s['sha256']} for s in trace['sources']]}


def build_manifest(input_path):
    source = Path(input_path).resolve()
    if not source.exists():
        raise ValueError('Input does not exist')
    files, traces, catalogs, bundle_manifests, issues = {}, [], [], [], []
    def issue(code, **data):
        issues.append({'code': code, **data})
    for name, declared_size, stream in file_records(source):
        if name in files:
            raise ValueError('Archive contains duplicate member paths')
        hasher, chunks, size = hashlib.sha256(), [], 0
        is_json = name.endswith('.json') and declared_size <= MAX_JSON_BYTES
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(block)
            size += len(block)
            if is_json:
                chunks.append(block)
        sha = hasher.hexdigest()
        files[name] = {'path': name, 'bytes': size, 'sha256': sha}
        if name.endswith('/INCOMPLETE') or name == 'INCOMPLETE':
            issue('incomplete_bundle', path=name)
        if not is_json:
            if name.endswith('.trace.json'):
                issue('trace_too_large', path=name)
            continue
        try:
            value = decode(b''.join(chunks))
        except (ValueError, UnicodeError):
            if name.endswith('.trace.json') or PurePosixPath(name).name == 'manifest.json':
                issue('invalid_json', path=name)
            continue
        if not isinstance(value, dict):
            if name.endswith('.trace.json'):
                issue('invalid_trace', path=name)
            continue
        version = value.get('schema_version', '')
        if PurePosixPath(name).name == 'manifest.json' and 'import_files' in value:
            bundle_manifests.append((name, value))
        elif version in ('trace-hunter/1.0', 'trace-hunter/1.1'):
            try:
                validate(value)
            except InvalidTrace:
                # Validator messages can include original input values; never copy them.
                issue('invalid_trace', path=name)
            else:
                traces.append(trace_metadata(value, name, sha, size))
        elif version == 'trace-hunter/catalog/1.0':
            try:
                validate_catalog(value)
            except InvalidTrace:
                issue('invalid_catalog', path=name)
            else:
                catalogs.append({'path': name, 'cases': [
                    {'query_id': c['query_id'], 'definition_digest': digest(canonical(c)),
                     'conversation_mode': c['conversation']['mode'],
                     'declared_user_turn_count': len(c['conversation']['turns']) if c['conversation']['mode'] != 'unknown' else None}
                    for c in value['cases']]})
        elif name.endswith('.trace.json'):
            issue('unsupported_trace_version', path=name)

    # Strip only a shared archive wrapper, so directory and packaged inventories agree.
    prefix = ''
    if not source.is_dir() and files:
        parts = {PurePosixPath(n).parts[0] for n in files}
        if len(parts) == 1 and all(len(PurePosixPath(n).parts) > 1 for n in files):
            prefix = next(iter(parts)) + '/'
    def clean(name):
        return name[len(prefix):] if prefix and name.startswith(prefix) else name
    files = {clean(n): {**f, 'path': clean(n)} for n, f in files.items()}
    for entry in traces + catalogs:
        entry['path'] = clean(entry['path'])
    for entry in issues:
        if 'path' in entry:
            entry['path'] = clean(entry['path'])

    for name, manifest in bundle_manifests:
        parent = PurePosixPath(clean(name)).parent
        if manifest.get('state') != 'ready':
            issue('bundle_not_ready', path=clean(name))
        for expected in manifest['import_files']:
            path = relative(str(parent / relative(expected['path'])))
            actual = files.get(path)
            if actual is None:
                issue('missing_import_file', path=path)
            elif any(actual[k] != expected.get(k) for k in ('sha256', 'bytes')):
                issue('import_digest_mismatch', path=path)

    cases = {}
    for catalog in catalogs:
        for case in catalog['cases']:
            previous = cases.get(case['query_id'])
            if previous and previous['definition_digest'] != case['definition_digest']:
                issue('conflicting_case_definition', query_id=case['query_id'])
            cases[case['query_id']] = case

    by_run, duplicates, source_files = {}, [], {}
    for trace in sorted(traces, key=lambda t: t['path']):
        for ref in trace['source_refs']:
            path = relative(str(PurePosixPath(trace['path']).parent / relative(ref['name'])))
            actual = files.get(path)
            source_files[path] = actual or {'path': path, 'bytes': None, 'sha256': None}
            if actual is None:
                issue('missing_source_file', path=path, run_id=trace['run_id'])
            elif actual['sha256'] != ref['sha256']:
                issue('source_digest_mismatch', path=path, run_id=trace['run_id'])
        case = cases.get(trace['query_id'], {})
        trace.update({'conversation_mode': case.get('conversation_mode', 'unknown'),
                      'declared_user_turn_count': case.get('declared_user_turn_count'),
                      'observed_user_turn_count': None})
        previous = by_run.get(trace['run_id'])
        if previous:
            same = previous['trace_digest'] == trace['trace_digest']
            duplicates.append({'run_id': trace['run_id'], 'path': trace['path'],
                               'first_path': previous['path'], 'kind': 'identical' if same else 'conflicting'})
            if not same:
                issue('conflicting_run_id', run_id=trace['run_id'], path=trace['path'])
        else:
            by_run[trace['run_id']] = trace
    runs = sorted(by_run.values(), key=lambda t: t['run_id'])
    if not runs:
        issue('no_standard_traces')
    for name, manifest in bundle_manifests:
        expected = manifest.get('runs', manifest.get('conversations'))
        if expected is not None:
            parent = PurePosixPath(clean(name)).parent
            actual = sum(PurePosixPath(t['path']).is_relative_to(parent) for t in runs)
            if expected != actual:
                issue('run_count_mismatch', path=clean(name), expected=expected, actual=actual)
    inventory = sorted(files.values(), key=lambda f: f['path'])
    tool_status = Counter()
    for run in runs:
        tool_status.update(run['tool_status'])
    summary = {key: sum(r[key] for r in runs) for key in (
        'record_count', 'tool_count', 'model_record_count', 'model_usage_record_count',
        'tool_time_known', 'tool_interval_known', 'phase_count')}
    summary.update({'run_count': len(runs), 'input_trace_file_count': len(traces),
                    'query_count': len({r['query_id'] for r in runs if r['query_id'] is not None}),
                    'tool_status': dict(sorted(tool_status.items())),
                    'error_run_count': sum(r['tool_status'].get('error', 0) > 0 for r in runs),
                    'multiple_fragment_run_count': sum((r['source_fragment_count'] or 0) > 1 for r in runs),
                    'declared_multi_turn_run_count': sum(r['conversation_mode'] == 'multi_turn' for r in runs),
                    'observed_user_turn_count': None,
                    'missing_model_coverage_runs': sum(r['coverage']['model_requests'] == 'missing' for r in runs),
                    'unknown_conversation_runs': sum(r['conversation_mode'] == 'unknown' for r in runs),
                    'duplicate_file_count': len(duplicates)})
    result = {'schema_version': VERSION, 'state': 'invalid' if issues else 'ready',
              'inventory_digest': digest(canonical(inventory)), 'files': inventory,
              'source_files': sorted(source_files.values(), key=lambda f: f['path']),
              'runs': runs, 'summary': summary, 'duplicates': duplicates,
              'issues': sorted(issues, key=lambda x: canonical(x)),
              'limits': ['Metadata only; original bodies are not copied.',
                         'Conversation counts are catalog declarations, not observed user turns.',
                         'Declared source bytes are hashed; JSON pointers and source semantics are not revalidated.',
                         'No database import, plugin execution, or historical command execution.']}
    result['corpus_digest'] = digest(canonical(result))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True, help='Prepared directory or tar/tar.gz package')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--output', type=Path, help='New manifest under this repository\'s ignored var directory')
    mode.add_argument('--check', type=Path, help='Compare against an existing fixed manifest')
    args = parser.parse_args(argv)
    try:
        if args.output:
            output = args.output.resolve()
            if not output.is_relative_to((ROOT / 'var').resolve()):
                raise ValueError('Output must be under the repository ignored var directory')
            if args.input.is_dir() and output.is_relative_to(args.input.resolve()):
                raise ValueError('Output must be outside the input corpus')
            if output.exists():
                raise ValueError('Output already exists; use --check or a new output path')
        result = build_manifest(args.input)
        if args.check:
            expected = decode(args.check.read_bytes())
            if expected != result:
                print(json.dumps({'state': 'mismatch', 'corpus_digest': result['corpus_digest']}))
                return 2
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open('x', encoding='utf-8') as stream:
                os.chmod(output, 0o600)
                json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write('\n')
        print(json.dumps({'state': result['state'], 'corpus_digest': result['corpus_digest'],
                          'summary': result['summary'], 'issue_count': len(result['issues'])}))
        return 0 if result['state'] == 'ready' else 2
    except (ValueError, OSError, tarfile.TarError, KeyError, TypeError):
        # Input-derived exception text can contain raw data or local secrets.
        print(json.dumps({'state': 'invalid', 'error': 'Cannot build manifest: check input structure, paths, and output destination.'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
