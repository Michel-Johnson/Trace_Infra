"""Extract bounded network timing evidence without copying credentials or bodies."""
import hashlib
import json
import math
import re
from pathlib import Path
from urllib.parse import urlsplit

STREAM_PATHS = {'/chat/completion', '/chat/async/chunk_stream',
                '/samantha/chat/completion', '/alice/message/stream_reply',
                '/alice/office/tool_local/chunk_stream'}
HOSTS = {'www.doubao.com', 'api5-normal-lq.doubao.com'}
METRIC_HEADERS = {'x-input-tokens', 'x-output-tokens', 'x-prompt-tokens',
                  'x-completion-tokens', 'x-total-tokens', 'x-reasoning-tokens'}


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def summarize(data, source_file, source_line):
    base = data.get('base') or {}
    url = urlsplit(base.get('origin_url', ''))
    if url.scheme != 'https' or url.hostname not in HOSTS or url.username is not None or url.path not in STREAM_PATHS:
        return None
    timing = data.get('timing') or {}
    request = timing.get('request') or {}
    detailed = timing.get('detailed_duration') or {}
    header = data.get('header') or {}
    response = data.get('response') or {}
    marks = []
    for part in str(header.get('server-timing', '')).split(','):
        match = re.match(r'\s*([\w.-]{1,64})\s*;\s*dur=(\d+(?:\.\d+)?)(?:\s*;|\s*$)', part)
        if match and number(float(match[2])) is not None:
            marks.append({'name': match[1], 'duration_ms': float(match[2])})
    metric_headers = {k: v for k, v in header.items() if k.lower() in METRIC_HEADERS
                      and isinstance(v, str) and re.fullmatch(r'\d{1,18}', v)}
    return {'source_file': source_file, 'source_line': source_line,
            'host': url.hostname, 'path': url.path, 'status_code': response.get('code'),
            'start_unix_s': number(request.get('start_time')),
            'log_unix_s': number(request.get('log_time')),
            'duration_ms': number(request.get('duration')),
            'http_ttfb_ms': number(detailed.get('ttfb')),
            'stream_body_window_ms': number(detailed.get('body_recv')),
            'phases_ms': {k: number(detailed.get(k)) for k in ('dns', 'tcp', 'ssl', 'send', 'header_recv')},
            'body_bytes': {k: number(response.get(k)) for k in ('sent_body_bytes', 'recv_body_bytes')},
            'server_timing': marks, 'metric_header_candidates': metric_headers,
            'model_duration_ms': None, 'actual_token_usage': None}


def audit(paths, start=None, end=None):
    records, sources, errors = [], [], 0
    for path in map(Path, paths):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 512 * 1024 * 1024:
            raise ValueError('Regular log files no larger than 512 MiB are required')
        before = path.stat()
        digest = hashlib.sha256()
        count = 0
        with path.open('rb') as stream:
            for line_number, line in enumerate(stream, 1):
                count += len(line)
                digest.update(line)
                if count > 512 * 1024 * 1024:
                    raise ValueError('Log grew past the audit limit')
                if b'[rlog]' not in line or b'request_log=' not in line:
                    continue
                try:
                    data = json.loads(line.split(b'request_log=', 1)[1])
                    if not isinstance(data, dict):
                        raise ValueError('object required')
                    record = summarize(data, path.name, line_number)
                except (ValueError, TypeError, AttributeError):
                    errors += 1
                    continue
                if record is None:
                    continue
                # Long-lived tool streams can start before the task. Select overlap.
                began, finished = record['start_unix_s'], record['log_unix_s']
                if start is not None and finished is not None and finished < start:
                    continue
                if end is not None and began is not None and began > end:
                    continue
                record['window_filter_unknown'] = (start is not None and finished is None) or (end is not None and began is None)
                records.append(record)
        after = path.stat()
        sources.append({'name': path.name, 'sha256': digest.hexdigest(), 'bytes_read': count,
                        'stable_during_read': (before.st_ino, before.st_size, before.st_mtime_ns) ==
                        (after.st_ino, after.st_size, after.st_mtime_ns) and count == before.st_size})
    return {'source': 'Doubao Work native AhaNetSDK request_log', 'sources': sources,
            'window_unix_s': [start, end], 'decode_errors': errors, 'requests': records,
            'limitations': ['HTTP TTFB is not model time to first token.',
                           'The body window includes model, tool, scheduling and transport waits.',
                           'Server-Timing phases may overlap; they are not summed.',
                           'A connection or task request is not a model request.',
                           'Tool streams overlapping a time window are not automatically assigned to its task.',
                           'No response bodies or authentication header values are exported.']}
