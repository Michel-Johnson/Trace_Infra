#!/usr/bin/env python3
"""Build a read-only gallery from an opt-in synthetic acceptance capture."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def build(capture_dir, output_dir):
    source, output = Path(capture_dir), Path(output_dir)
    capture = json.loads((source / 'capture.json').read_text())
    if capture.get('synthetic_inputs') is not True or capture.get('model_calls_executed') != 0:
        raise ValueError('This gallery only accepts synthetic acceptance captures')
    output.mkdir(parents=True, exist_ok=True)
    # Copy exact captured bytes; never rewrite a trace to make its query match an objective.
    refs = [capture['report']]
    for case in capture['cases'].values():
        for event in case['events']:
            refs.extend(ref for ref in (event['request'], event['response']) if ref)
    for ref in refs:
        path = Path(ref['path'])
        if path.parent != Path('objects'):
            raise ValueError('Invalid capture object path')
        raw = (source / path).read_bytes()
        if 'sha256:' + hashlib.sha256(raw).hexdigest() != ref['digest']:
            raise ValueError('Capture digest mismatch: ' + str(path))
        (output / path).parent.mkdir(exist_ok=True)
        shutil.copyfile(source / path, output / path)

    parsed = {}
    def read(ref):
        if not ref: return None
        key = ref['path']
        if key not in parsed:
            try: parsed[key] = json.loads((source / key).read_bytes())
            except (ValueError, UnicodeError): parsed[key] = None
        return parsed[key]

    report = read(capture['report'])
    objectives = json.loads((ROOT / 'examples/platform-acceptance/queries.json').read_text())['queries']
    traces, direct = {}, {}
    for case_id, case in capture['cases'].items():
        direct[case_id] = set()
        for event in case['events']:
            path = urlsplit(event.get('path', '')).path
            if event.get('method') != 'POST' or not path.endswith('/traces') or event['status'] not in (200, 201):
                continue
            doc, result = read(event['request']), read(event['response'])
            if not isinstance(doc, dict) or 'spans' not in doc: continue
            revision = result['revision']['revision']
            digest = event['request']['digest']
            run = doc['run']
            queries = []
            if run.get('query') is not None:
                queries.append({'source': 'run.query', 'text': run['query']})
            for message in doc.get('messages', []):
                if message.get('role') != 'user': continue
                content = message.get('content')
                value = content.get('value') if isinstance(content, dict) else content
                if value is not None:
                    queries.append({'source': 'messages/' + message['id'], 'turn_id': message.get('turn_id'), 'text': value})
            traces[digest] = {'run_id': run['id'], 'query_id': run.get('query_id'),
                'revision': revision, 'queries': queries, 'record_count': len(doc['spans']),
                'message_count': len(doc.get('messages', [])), 'harness': run.get('harness'),
                'model': run.get('model'), 'content': event['request']}
            direct[case_id].add(digest)

    by_version = {(t['run_id'], t['revision']): d for d, t in traces.items()}
    def trace_refs(value, found):
        if isinstance(value, dict):
            if value.get('kind') == 'trace_revision':
                key = (value.get('run_id') or value.get('id'), value.get('revision'))
                digest = by_version.get(key)
                if digest: found.add(digest)
            if isinstance(value.get('run_id'), str) and isinstance(value.get('revision'), int):
                digest = by_version.get((value['run_id'], value['revision']))
                if digest: found.add(digest)
            for v in value.values(): trace_refs(v, found)
        elif isinstance(value, list):
            for v in value: trace_refs(v, found)

    index = {'schema_version': 'trace-hunter/case-gallery/1', 'synthetic_inputs': True,
        'model_calls_executed': 0, 'database': capture['database'], 'started_at': capture['started_at'],
        'summary': report['summary'], 'report': capture['report'], 'cases': [],
        'planned_cases': report['planned_cases']}
    (output / 'cases').mkdir(exist_ok=True)
    for case in report['cases']:
        cid = case['case_id']
        events = capture['cases'][cid]['events']
        found = set(direct[cid])
        for event in events:
            trace_refs(read(event['request']), found)
            trace_refs(read(event['response']), found)
        inputs = [traces[d] for d in sorted(found, key=lambda d: (d not in direct[cid], traces[d]['run_id'], traces[d]['revision']))]
        item = {'case_id': cid, 'title': case['title'], 'objective': objectives.get(cid, ''),
            'status': case['status'], 'trace_count': len(inputs), 'event_count': len(events),
            'check_count': len(case['checks']), 'path': 'cases/' + cid + '.json'}
        detail = {**item, 'traces': inputs, 'events': events, 'checks': case['checks'],
            'elapsed_ms': max((e['start_ms'] + e['duration_ms'] for e in events), default=0)}
        (output / item['path']).write_text(json.dumps(detail, ensure_ascii=False) + '\n')
        index['cases'].append(item)
    (output / 'index.json').write_text(json.dumps(index, ensure_ascii=False, indent=2) + '\n')
    return index


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture_dir')
    parser.add_argument('--output', default=str(ROOT / 'apps/web/public/case-gallery-data'))
    args = parser.parse_args()
    index = build(args.capture_dir, args.output)
    print(json.dumps({'output': args.output, 'cases': len(index['cases']), 'planned': len(index['planned_cases'])}))
