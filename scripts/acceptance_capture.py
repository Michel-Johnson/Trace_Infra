"""Opt-in, local evidence capture for the synthetic acceptance suite only.

Never records authentication headers or credential/bootstrap endpoints. Raw
objects remain outside Git. This is not a general production trace collector.
"""
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit


class AcceptanceCapture:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / 'objects').mkdir(exist_ok=True)
        self.cases = {}

    def begin(self, case_id):
        self.case_id = case_id
        self.started = time.perf_counter()
        self.cases[case_id] = {'events': []}

    def blob(self, raw, media):
        digest = hashlib.sha256(raw).hexdigest()
        try:
            json.loads(raw)
            suffix = '.json'
        except (ValueError, UnicodeError):
            suffix = '.bin'
        path = 'objects/' + digest + suffix
        target = self.directory / path
        if not target.exists(): target.write_bytes(raw)
        return {'path': path, 'digest': 'sha256:' + digest, 'size_bytes': len(raw), 'media_type': media}

    def http(self, stage, response, started, expected_status=200):
        url = urlsplit(str(response.request.url))
        # The runner establishes credentials separately; never turn this into an auth recorder.
        if '/credentials' in url.path or '/principals' in url.path:
            raise ValueError('Credential endpoints do not belong in this evidence capture')
        request = response.request
        events = self.cases[self.case_id]['events']
        events.append({'sequence': len(events) + 1, 'kind': 'http', 'stage': stage,
            'method': request.method, 'path': url.path + ('?' + url.query if url.query else ''),
            'status': response.status_code, 'expected_status': expected_status,
            'start_ms': round((started - self.started) * 1000, 3),
            'duration_ms': round((time.perf_counter() - started) * 1000, 3),
            'request': self.blob(request.content, request.headers.get('content-type', 'application/octet-stream')) if request.content else None,
            'response': self.blob(response.content, response.headers.get('content-type', 'application/octet-stream'))})

    def worker(self, invocation, result, started, expected_status='succeeded'):
        events = self.cases[self.case_id]['events']
        events.append({'sequence': len(events) + 1, 'kind': 'worker', 'stage': 'worker',
            'operation': invocation['operation']['operation_id'], 'status': result['state'], 'expected_status': expected_status,
            'start_ms': round((started - self.started) * 1000, 3),
            'duration_ms': round((time.perf_counter() - started) * 1000, 3),
            'request': self.blob(json.dumps(invocation, ensure_ascii=False).encode(), 'application/json'),
            'response': self.blob(json.dumps(result, ensure_ascii=False).encode(), 'application/json')})

    def finish(self, report):
        manifest = {'schema_version': 'trace-hunter/acceptance-capture/1', 'synthetic_inputs': True,
            'model_calls_executed': 0, 'database': report['database'], 'started_at': report['started_at'],
            'report': self.blob(json.dumps(report, ensure_ascii=False).encode(), 'application/json'),
            'cases': self.cases}
        (self.directory / 'capture.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
