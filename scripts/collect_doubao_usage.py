#!/usr/bin/env python3
"""Read the current user's usage ledger from an already enabled local Doubao renderer.

This supplement contains quota percentages, not inferred token counts. No restart,
task submission, pagination, login, permissions changes, or platform import occurs.
"""
import argparse
import asyncio
import hashlib
import json
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from capture_doubao_cdp import is_doubao_chat_page, private_json


async def collect(args):
    import websockets

    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest = {'state': 'starting', 'source': 'client AGWGetUsageTimeline',
                'chunk_key': args.chunk_key, 'api_module': args.api_module,
                'request': {'page_size': 20}, 'actual_token_usage': None,
                'model_duration_ms': None, 'issues': []}
    private_json(args.output / 'manifest.json', manifest)
    key = '__traceHunterUsage_' + uuid.uuid4().hex
    access = 'globalThis[' + json.dumps(key) + ']'
    template = Path(__file__).with_name('doubao_usage_probe.js').read_text()
    code = template.replace('__PROBE_KEY__', json.dumps(key)).replace(
        '__CHUNK_KEY__', json.dumps(args.chunk_key)).replace('__API_MODULE__', str(args.api_module))
    manifest['probe_sha256'] = hashlib.sha256(template.encode()).hexdigest()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/json/list', timeout=3) as response:
            targets = json.loads(response.read(2 * 1024 * 1024))
        targets = [t for t in targets if t.get('type') == 'page' and t.get('url') == args.target_url]
        if len(targets) != 1:
            raise ValueError('Exactly one selected Doubao chat renderer required')
        endpoint = targets[0]['webSocketDebuggerUrl']
        url = urlsplit(endpoint)
        if url.scheme != 'ws' or url.hostname not in ('127.0.0.1', 'localhost') or url.port != args.port or url.username is not None:
            raise ValueError('Selected loopback debugger endpoint required')
        async with websockets.connect(endpoint, max_size=8 * 1024 * 1024, open_timeout=3) as ws:
            serial = 0

            async def evaluate(expression):
                nonlocal serial
                serial += 1
                await ws.send(json.dumps({'id': serial, 'method': 'Runtime.evaluate', 'params': {
                    'expression': expression, 'returnByValue': True, 'timeout': 3000}}))
                while True:
                    reply = json.loads(await asyncio.wait_for(ws.recv(), 5))
                    if reply.get('id') == serial:
                        if 'error' in reply or reply.get('result', {}).get('exceptionDetails'):
                            raise RuntimeError('Usage probe evaluation failed')
                        return reply.get('result', {}).get('result', {}).get('value')

            try:
                manifest['installation'] = await evaluate(code)
                deadline = time.monotonic() + 20
                while True:
                    result = await evaluate('(() => { const s=' + access + '; return !s?{missing:true}:!s.done?{done:false}:{done:true,started_at_ms:s.started_at_ms,finished_at_ms:s.finished_at_ms,response:s.response,error_type:s.error_type}; })()')
                    if result.get('missing'):
                        raise RuntimeError('Usage probe disappeared before collection')
                    if result.get('done'):
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Usage query did not complete within 20 seconds')
                    await asyncio.sleep(.25)
                private_json(args.output / 'response.json', result)
                raw = (args.output / 'response.json').read_bytes()
                manifest['response_sha256'] = hashlib.sha256(raw).hexdigest()
                response = result.get('response')
                if result.get('error_type'):
                    manifest['state'] = 'query_failed'
                    manifest['issues'].append('client_query_failed')
                elif not isinstance(response, dict) or response.get('code') != 0:
                    manifest['state'] = 'query_failed'
                    manifest['issues'].append('server_did_not_return_success')
                else:
                    data = response.get('data') or {}
                    manifest.update(state='collected', entry_count=len(data.get('entries') or []),
                                    has_more=data.get('has_more'),
                                    consumption_column_description=data.get('consumption_column_description'))
            finally:
                try:
                    manifest['probe_removed'] = await evaluate('(() => { const s=' + access + '; if(s){clearTimeout(s.cleanup_timer);delete ' + access + ';}return !Object.hasOwn(globalThis,' + json.dumps(key) + ');})()')
                except Exception:
                    manifest['issues'].append('cleanup_unverified_60_second_expiry')
    except Exception as error:
        manifest['state'] = 'failed'
        manifest['issues'].append(type(error).__name__)
        raise
    finally:
        private_json(args.output / 'manifest.json', manifest)
        print(json.dumps({k: manifest.get(k) for k in ('state', 'entry_count', 'has_more', 'probe_removed', 'issues')}, ensure_ascii=False))
    return manifest['state'] == 'collected'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=9222)
    parser.add_argument('--target-url', required=True)
    parser.add_argument('--chunk-key', default='@flow-web/desktop:stable')
    parser.add_argument('--api-module', type=int, default=359531)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not is_doubao_chat_page(args.target_url) or not 1 <= args.port <= 65535 or args.api_module < 0:
        parser.error('Invalid target, port or module ID')
    raise SystemExit(0 if asyncio.run(collect(args)) else 1)


if __name__ == '__main__':
    main()
