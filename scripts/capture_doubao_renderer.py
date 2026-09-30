#!/usr/bin/env python3
"""Capture Doubao's custom fetch stream from an already enabled loopback renderer.

Start before submitting a task. Does not restart apps or enable debugging.
Original function results and reads are preserved; response contents stay local.
Dependencies: requirements-capture.txt. See docs/local-doubao-renderer-hook.md.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from capture_doubao_cdp import is_doubao_capture_page, private_json


async def capture(args):
    import websockets

    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    key = '__traceHunter_' + uuid.uuid4().hex
    template = Path(__file__).with_name('doubao_renderer_hook.js').read_text()
    script = template.replace('__TRACE_HUNTER_CONTROL_KEY__', json.dumps(key)).replace('__TRACE_HUNTER_TIMEOUT_MS__', str(args.seconds*1000))
    access = 'globalThis[' + json.dumps(key) + ']'
    manifest = {'query_id': args.query_id, 'env_id': args.env_id,
                'started_at': datetime.now(timezone.utc).isoformat(), 'state': 'starting',
                'source': 'client custom fetch / original default reader observations',
                'hook_sha256': hashlib.sha256(template.encode()).hexdigest(),
                'control_key': key, 'semantic_trace_complete': None, 'issues': [],
                'limits': {'seconds': args.seconds, 'queue_mib': 8, 'event_mib': 2, 'total_mib': 32},
                'coverage': 'selected chat or background renderer; selected Doubao HTTPS hosts; original default reader only; native-only traffic can bypass this hook',
                'target_url': args.target_url,
                'selected_paths': ['/chat/completion', '/chat/async/chunk_stream', '/samantha/chat/completion', '/alice/message/stream_reply', '/alice/office/tool_local/chunk_stream'],
                'stop_policy': {'seconds': args.seconds, 'stop_after_streams': args.stop_after_streams,
                                'note': 'Reader EOF and stream count do not prove semantic task completion.'}}
    private_json(args.output / 'manifest.json', manifest)
    serial, count, ended, digest = 0, 0, set(), hashlib.sha256()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/json/list', timeout=3) as f:
            targets = json.loads(f.read(2*1024*1024))
        targets = [t for t in targets if t.get('type') == 'page' and t.get('url') == args.target_url]
        if len(targets) != 1:
            raise ValueError('Exactly one selected Doubao chat renderer required')
        endpoint = targets[0]['webSocketDebuggerUrl']; u = urlsplit(endpoint)
        if u.scheme != 'ws' or u.hostname not in ('127.0.0.1', 'localhost') or u.port != args.port or u.username is not None:
            raise ValueError('Selected loopback debugger endpoint required')
        async with websockets.connect(endpoint, max_size=16*1024*1024, open_timeout=3) as ws:
            async def evaluate(expression):
                nonlocal serial
                serial += 1
                await ws.send(json.dumps({'id': serial, 'method': 'Runtime.evaluate', 'params': {
                    'expression': expression, 'returnByValue': True, 'timeout': 3000}}))
                while True:
                    response = json.loads(await asyncio.wait_for(ws.recv(), 5))
                    if response.get('id') == serial:
                        if 'error' in response or response.get('result', {}).get('exceptionDetails'):
                            raise RuntimeError('Renderer hook evaluation failed')
                        return response.get('result', {}).get('result', {}).get('value')
            installed = False
            install_attempted = False
            files = {}
            try:
                install_attempted = True
                manifest['installation'] = await evaluate(script)
                installed = True
                manifest['state'] = 'recording'
                private_json(args.output / 'manifest.json', manifest)
                fd = os.open(args.output / 'events.jsonl', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as output:
                    def persist(batch):
                        nonlocal count
                        for text in batch['events']:
                            event = json.loads(text)
                            raw = (text + '\n').encode()
                            output.write(raw); digest.update(raw); count += 1
                            if event['type'] == 'stream_end': ended.add(event['request'])
                            if event['type'] == 'stream_chunk':
                                rid = event['request']
                                if type(rid) is not int or not 1 <= rid <= 10000: raise ValueError('Invalid request identity')
                                data = base64.b64decode(event['bytes_b64'], validate=True)
                                if len(data) != event['length']: raise ValueError('Chunk length mismatch')
                                if rid not in files:
                                    fd = os.open(args.output / f'response-{rid}.sse', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                                    files[rid] = os.fdopen(fd, 'wb')
                                files[rid].write(data); files[rid].flush()
                        output.flush()
                        manifest['final_status'] = batch['status']
                    print('READY: renderer hook installed; submit the task in Doubao now.', flush=True)
                    deadline, report_at = time.monotonic() + args.seconds, time.monotonic() + 30
                    try:
                        while time.monotonic() < deadline and not (args.output / 'STOP').exists():
                            batch = await evaluate(access + '.drain()'); persist(batch)
                            if not batch['status']['enabled']:
                                manifest['issues'].append('hook_timeout'); break
                            if args.stop_after_streams and len(ended) >= args.stop_after_streams: break
                            if time.monotonic() >= report_at:
                                print(json.dumps({'events': count, 'counts': batch['status']['counts']}), flush=True)
                                report_at = time.monotonic() + 30
                            await asyncio.sleep(.25)
                    finally:
                        manifest['cleanup'] = await evaluate(access + '.stop()')
                        persist(await evaluate(access + '.drain()'))
                        output.flush(); os.fsync(output.fileno())
            finally:
                if install_attempted and 'cleanup' not in manifest:
                    try: manifest['cleanup'] = await evaluate(access + '.stop()')
                    except Exception: manifest['issues'].append('cleanup_unverified_wait_for_hook_timeout_or_restart')
                for f in files.values(): f.close()
                if (manifest.get('cleanup') or {}).get('enabled') is False:
                    try: await evaluate('delete ' + access)
                    except Exception: manifest['issues'].append('inactive_control_object_retained')
    except Exception as error:
        manifest['issues'].append(type(error).__name__)
        raise
    finally:
        manifest.update(state='stopped', ended_at=datetime.now(timezone.utc).isoformat(),
                        events=count, ended_reader_requests=sorted(ended), events_sha256=digest.hexdigest())
        counts = manifest.get('final_status', {}).get('counts', {})
        restored = (manifest.get('cleanup') or {}).get('restored', {})
        if restored and not all(restored.values()): manifest['issues'].append('function_restore_conflict')
        if 'final_status' not in manifest: manifest['issues'].append('hook_status_unavailable')
        elif not counts.get('target_fetches'): manifest['issues'].append('no_matching_fetch')
        if counts.get('dropped'): manifest['issues'].append('observer_dropped_events')
        if counts.get('target_fetches', 0) != len(ended): manifest['issues'].append('reader_coverage_incomplete')
        private_json(args.output / 'manifest.json', manifest)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', type=int, default=9222)
    p.add_argument('--target-url', required=True)
    p.add_argument('--query-id', required=True)
    p.add_argument('--env-id', required=True)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--seconds', type=int, default=600)
    p.add_argument('--stop-after-streams', type=int, default=0,
                   help='Default 0: wait for duration or STOP, including continuation streams. Nonzero is a diagnostic stream limit, not task completion.')
    args=p.parse_args()
    if not is_doubao_capture_page(args.target_url) or not 1<=args.port<=65535 or not 1<=args.seconds<=7200 or not 0<=args.stop_after_streams<=100:
        p.error('Invalid Doubao target, port or capture limits')
    asyncio.run(capture(args))


if __name__=='__main__': main()
