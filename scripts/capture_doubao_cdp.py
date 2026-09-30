#!/usr/bin/env python3
"""Observe a Doubao chat renderer using an already enabled loopback CDP port.

Does not launch/restart the client, enable debugging, read cookies, evaluate page
code, intercept requests, or replay tasks. Start this before submitting a query.
Requires the websockets package. Raw response contents stay in a private folder.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

def matches(url, host):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.hostname == host and parsed.path == '/chat/completion'
                and parsed.username is None and parsed.password is None)
    except (TypeError, ValueError):
        return False


def is_doubao_chat_page(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme in ('chrome', 'doubaowork') and parsed.netloc == 'doubaowork-chat'
                and (parsed.path == '/chat' or parsed.path.startswith('/chat/')))
    except (TypeError, ValueError):
        return False


def is_doubao_capture_page(url):
    if is_doubao_chat_page(url):
        return True
    try:
        parsed = urlsplit(url)
        return (parsed.scheme in ('chrome', 'doubaowork') and parsed.netloc == 'doubaowork-background'
                and parsed.path == '/' and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError):
        return False


def private_json(path, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    with os.fdopen(os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600), 'wb') as f:
        f.write(raw)


def safe_metadata(method, p):
    """Intentionally omit request/response headers, cookies, query strings and POST data."""
    keys = ('requestId', 'timestamp', 'wallTime', 'type', 'dataLength', 'encodedDataLength', 'canceled')
    result = {k: p[k] for k in keys if k in p}
    if method == 'Network.requestWillBeSent':
        request = p.get('request', {})
        parsed = urlsplit(request.get('url', ''))
        origin = f'{parsed.scheme}://{parsed.hostname}'
        if parsed.port is not None:
            origin += f':{parsed.port}'
        result.update(method=request.get('method'), origin=origin, path=parsed.path,
                      redirect='redirectResponse' in p)
    if method == 'Network.responseReceived':
        response = p.get('response', {})
        result.update({k: response.get(k) for k in ('status', 'mimeType', 'protocol')})
    return result


async def capture(args):
    import websockets

    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest = {'state': 'starting', 'started_at': datetime.now(timezone.utc).isoformat(),
                'query_id': args.query_id, 'env_id': args.env_id,
                'capture_complete': False, 'semantic_trace_complete': None,
                'source': 'CDP client observations, not server model spans',
                'limits': {'seconds': args.seconds, 'bytes': args.max_mib*1024*1024},
                'issues': [], 'matching_requests': 0, 'events': 0, 'bytes': 0,
                'request_headers_or_credentials_captured': False,
                'client_restart_or_settings_change': False}
    private_json(args.output / 'manifest.json', manifest)
    hashes = hashlib.sha256()
    serial, selected, pending = 0, set(), {}
    enabled = False
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/json/list', timeout=3) as response:
            targets = json.load(response)
        targets = [t for t in targets if t.get('type') == 'page' and t.get('url') == args.target_url]
        if len(targets) != 1:
            raise ValueError('Exactly one matching Doubao chat page is required')
        endpoint = targets[0]['webSocketDebuggerUrl']
        url = urlsplit(endpoint)
        if (url.scheme != 'ws' or url.hostname not in ('127.0.0.1', 'localhost') or url.port != args.port
                or url.username is not None or url.password is not None):
            raise ValueError('Only the selected loopback debugger endpoint is allowed')
        async with websockets.connect(endpoint, max_size=16*1024*1024, open_timeout=3) as ws:
            async def send(method, params=None, request_id=None):
                nonlocal serial
                serial += 1
                pending[serial] = (method, request_id)
                await ws.send(json.dumps({'id': serial, 'method': method, 'params': params or {}}))
                return serial
            await send('Network.enable', {'maxTotalBufferSize': 8*1024*1024, 'maxResourceBufferSize': 4*1024*1024})
            deadline = time.monotonic() + args.seconds
            fd = os.open(args.output / 'events.jsonl', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as output:
                def record(kind, payload):
                    row = {'sequence': manifest['events'], 'kind': kind, 'observed_mono_ns': time.monotonic_ns(), **payload}
                    raw = (json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n').encode()
                    if manifest['bytes'] + len(raw) > args.max_mib*1024*1024:
                        if 'byte_limit' not in manifest['issues']: manifest['issues'].append('byte_limit')
                        return
                    output.write(raw); output.flush(); hashes.update(raw)
                    manifest['bytes'] += len(raw); manifest['events'] += 1
                try:
                    while time.monotonic() < deadline and not (args.output / 'STOP').exists():
                        try:
                            event = json.loads(await asyncio.wait_for(ws.recv(), min(1, max(.01, deadline-time.monotonic()))))
                        except asyncio.TimeoutError:
                            continue
                        if 'id' in event:
                            method, rid = pending.pop(event['id'], ('unknown', None))
                            if 'error' in event:
                                manifest['issues'].append(method + '_failed')
                                record('diagnostic_error', {'method': method, 'requestId': rid, 'code': event['error'].get('code')})
                                if method == 'Network.enable': break
                            elif method == 'Network.enable':
                                enabled = True; manifest['state'] = 'recording'
                                private_json(args.output / 'manifest.json', manifest)
                                print('READY: network observation enabled; submit the test in Doubao now.', flush=True)
                            elif method == 'Network.streamResourceContent':
                                buffered = event.get('result', {}).get('bufferedData', '')
                                base64.b64decode(buffered, validate=True)
                                record('buffered_prefix', {'requestId': rid, 'bytes_b64': buffered,
                                                          'original_arrival_time_unknown': True})
                            continue
                        method, p = event.get('method', ''), event.get('params', {})
                        rid = p.get('requestId')
                        if method == 'Network.requestWillBeSent' and matches(p.get('request', {}).get('url'), args.host):
                            selected.add(rid)
                            manifest['matching_requests'] += 1
                        if rid not in selected: continue
                        if method in ('Network.requestWillBeSent', 'Network.responseReceived', 'Network.dataReceived', 'Network.loadingFinished', 'Network.loadingFailed'):
                            metadata = safe_metadata(method, p)
                            if method == 'Network.dataReceived' and 'data' in p:
                                base64.b64decode(p['data'], validate=True)
                                metadata['bytes_b64'] = p['data']
                            record(method, metadata)
                            if method == 'Network.responseReceived':
                                await send('Network.streamResourceContent', {'requestId': rid}, rid)
                            if method == 'Network.loadingFailed': manifest['issues'].append('request_loading_failed')
                            if method in ('Network.loadingFinished', 'Network.loadingFailed'): selected.discard(rid)
                finally:
                    # Disable on this diagnostic connection only. Never cancel the application request.
                    if enabled:
                        await ws.send(json.dumps({'id': serial+1, 'method': 'Network.disable'}))
                    output.flush(); os.fsync(output.fileno())
                    if selected: manifest['issues'].append('capture_stopped_with_active_requests')
                    if pending: manifest['issues'].append('unresolved_diagnostic_commands')
    except Exception as error:
        manifest['issues'].append(type(error).__name__)
        raise
    finally:
        manifest.update(state='stopped', ended_at=datetime.now(timezone.utc).isoformat(), sha256=hashes.hexdigest())
        if not manifest['matching_requests']: manifest['issues'].append('no_matching_request_observed')
        # This probe deliberately makes no assertion of complete semantic/network coverage.
        private_json(args.output / 'manifest.json', manifest)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', type=int, default=9222)
    p.add_argument('--target-url', required=True)
    p.add_argument('--host', default='api5-normal-lq.doubao.com')
    p.add_argument('--query-id', required=True)
    p.add_argument('--env-id', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--seconds', type=int, default=600)
    p.add_argument('--max-mib', type=int, default=64)
    args = p.parse_args()
    if not is_doubao_chat_page(args.target_url):
        p.error('Only the explicitly selected Doubao chat page is allowed')
    if not (1 <= args.port <= 65535 and 1 <= args.seconds <= 1800 and 1 <= args.max_mib <= 128):
        p.error('Invalid port, duration or byte limit')
    asyncio.run(capture(args))


if __name__ == '__main__':
    main()
