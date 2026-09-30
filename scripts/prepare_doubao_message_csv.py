#!/usr/bin/env python3
"""Prepare a message-level Doubao CSV as a self-contained import bundle."""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trace_hunter.bundle_integrity import BUNDLE_VERSION, file_entry, verify_metadata
from trace_hunter.catalog import validate_catalog
from trace_hunter.corpus_import import digest
from trace_hunter.doubao_message_csv import (
    FORMAT,
    VERSION,
    inspect_csv,
    normalize_record,
    require_valid_csv,
)
from trace_hunter.import_bundle import MAX_IMPORT_BYTES, verify_imports
from prepare_doubao_corpus import encode, private_write


def private_copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    hasher = hashlib.sha256()
    size = 0
    with Path(source).open('rb') as incoming, destination.open('xb') as outgoing:
        destination.chmod(0o600)
        for block in iter(lambda: incoming.read(1024 * 1024), b''):
            outgoing.write(block)
            hasher.update(block)
            size += len(block)
    return {'sha256': hasher.hexdigest(), 'bytes': size}


def make_catalog(cases, source_sha):
    value = {
        'schema_version': 'trace-hunter/catalog/1.0',
        'collection': {
            'id': f'doubao-message-csv-{source_sha[:12]}-normalized-v{VERSION.replace(".", "_")}',
            'title': 'Doubao Work · 消息级测试样本',
            'kind': 'task',
            'description': ('每个 CSV 行是一份回复样本；精确相同的脱敏 prompt 共享 query_id。'
                            '累计运行时消息仅用于恢复带稳定身份的当前模型与工具事件，'
                            '缺失的逐请求明细不会从聚合计数或 token 中推测。'),
        },
        'cases': [cases[key] for key in sorted(cases)],
    }
    validate_catalog(value)
    return value


def prepare(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Use a new output directory; previous bundles are immutable')
    output.mkdir(parents=True, mode=0o700)
    private_write(output / 'INCOMPLETE', b'Conversion or verification has not finished.\n')
    import_root = output / 'import'
    raw_root = import_root / 'raw'
    raw_name = 'source.csv'
    raw_entry = private_copy(source, raw_root / raw_name)
    source_sha = raw_entry['sha256']
    files = []
    records = []
    cases = {}
    totals = collections.Counter()

    def convert(row_number, row, document, events):
        trace, index, case = normalize_record(
            row, row_number, source_sha, raw_name, events=events, document=document)
        raw = encode(trace)
        if len(raw) > MAX_IMPORT_BYTES:
            raise ValueError('Normalized response exceeds 16 MiB import limit: ' + trace['run']['id'])
        name = trace['run']['id'] + '.trace.json'
        private_write(import_root / name, raw)
        files.append({'path': 'import/' + name, 'sha256': digest(raw), 'bytes': len(raw),
                      'run_id': trace['run']['id'], 'query_id': trace['run']['query_id']})
        records.append(index)
        existing = cases.get(case['query_id'])
        if existing is not None and existing != case:
            raise ValueError('Query hash collision produced different case definitions')
        cases[case['query_id']] = case
        totals['runs'] += 1
        totals['model_spans'] += index['model_spans']
        totals['tool_spans'] += index['tool_spans']
        totals['model_coverage_complete'] += index['coverage']['model_requests'] == 'complete'
        totals['model_coverage_partial'] += index['coverage']['model_requests'] == 'partial'
        totals['model_coverage_missing'] += index['coverage']['model_requests'] == 'missing'
        totals['tool_coverage_complete'] += index['coverage']['tools'] == 'complete'
        totals['tool_coverage_partial'] += index['coverage']['tools'] == 'partial'
        totals['tool_coverage_missing'] += index['coverage']['tools'] == 'missing'
        if row_number % 250 == 0:
            print(f'Normalized {row_number} response rows', flush=True)

    report = inspect_csv(raw_root / raw_name, convert)
    private_write(output / 'source-audit.json', encode(report))
    require_valid_csv(report)
    if totals['runs'] != report['scope']['source_rows']:
        raise ValueError('CSV audit and normalized run counts differ')
    catalog = make_catalog(cases, source_sha)
    catalog_raw = encode(catalog)
    if len(catalog_raw) > MAX_IMPORT_BYTES:
        raise ValueError('Catalog exceeds 16 MiB import limit')
    private_write(import_root / 'catalog.json', catalog_raw)
    files.insert(0, {'path': 'import/catalog.json', 'sha256': digest(catalog_raw),
                     'bytes': len(catalog_raw), 'kind': 'catalog'})
    private_write(output / 'record-index.json', encode(records))
    print(f"Normalized {totals['runs']} runs; verifying imports", flush=True)
    verification = verify_imports(
        output, files, chronological=False,
        progress=lambda count: print(f"Verified {count}/{totals['runs']}", flush=True))
    if verification['runs_imported_to_temporary_db'] != totals['runs']:
        raise ValueError('Temporary import run count mismatch')
    manifest = {
        'state': 'ready',
        'bundle_schema_version': BUNDLE_VERSION,
        'source_format': FORMAT,
        'converter_version': VERSION,
        'schema_version': 'trace-hunter/1.1',
        'source_file': source.name,
        'source_file_sha256': source_sha,
        'source_rows': totals['runs'],
        'runs': totals['runs'],
        'conversations': report['scope']['conversations'],
        'query_groups': len(cases),
        'collection_id': catalog['collection']['id'],
        'counts': dict(totals),
        'count_semantics': ('Each CSV row is one response run. Model/tool span counts include only current '
                            'events with explicit per-event identity; aggregate-only activity remains evidence.'),
        'import_files': files,
        'raw_sources': {raw_name: raw_entry},
        'auxiliary_files': [
            file_entry(output, 'record-index.json', 'record_index'),
            file_entry(output, 'source-audit.json', 'source_audit'),
        ],
        'validation': verification,
        'identity_policy': ('enc_bot_message_id identifies a run; exact masked prompt bytes identify a query. '
                            'Conversation and section IDs remain provenance, not run grouping.'),
        'timing_policy': ('Source start_time and duration_ms are retained independently. Cumulative history '
                          'without current event identity is not imported as a new event.'),
    }
    verification.update(verify_metadata(output, manifest))
    private_write(output / 'manifest.json', encode(manifest))
    readme = f'''# Doubao Work message CSV import bundle

Source rows: {totals['runs']:,}
Source conversations: {report['scope']['conversations']:,}
Exact prompt query groups: {len(cases):,}
Observed model spans: {totals['model_spans']:,}
Observed tool spans: {totals['tool_spans']:,}

Each CSV row is one response run, identified by enc_bot_message_id. Exact masked
prompt bytes share a query_id. The source message list is cumulative; historical
messages are retained in raw/source.csv and are not counted again as current
events. Aggregate model counts or token usage without a matching per-request
event stay in source-row evidence and make model coverage partial.

Import manifest.import_files in order. Import and browsing do not trigger
analysis. The raw CSV is private evidence and must not be committed.

Source SHA-256: {source_sha}
'''
    private_write(output / 'README.md', readme.encode())
    (output / 'INCOMPLETE').unlink()
    result = {'output': str(output), 'runs': totals['runs'],
              'conversations': report['scope']['conversations'],
              'query_groups': len(cases), 'counts': dict(totals), 'state': 'ready'}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source, args.output)


if __name__ == '__main__':
    main()
