#!/usr/bin/env python3
"""Exercise the byte journal through real loopback sockets using synthetic SSE.

No configurable destination: this probe cannot attach to Doubao, set a proxy,
install certificates, or send traffic to external hosts.
"""
import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trace_hunter.wire_capture import WireCapture


async def exercise(folder, mode):
    journal = WireCapture(folder, query_id='synthetic-sse-probe',
                          env_id='loopback-probe', max_bytes=32 if mode == 'quota' else 1048576)
    first_seen = asyncio.Event()
    relay_done = asyncio.Event()
    result = {'mode': mode, 'synthetic': True}
    expected_request = b'POST /events HTTP/1.1\r\nHost: probe.local\r\nContent-Length: 5\r\n\r\nhello'
    first = 'data: {"text":"中文首包"}\n\n'.encode()
    last = b'data: [DONE]\n\n'
    headers = b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n'
    def chunk(body):
        return f'{len(body):x}\r\n'.encode() + body + b'\r\n'
    expected_response = headers + chunk(first) + chunk(last) + b'0\r\n\r\n'
    upstream_received = bytearray()
    downstream_received = bytearray()
    relay_errors = []

    async def upstream(reader, writer):
        try:
            upstream_received.extend(await reader.readuntil(b'\r\n\r\n'))
            upstream_received.extend(await reader.readexactly(5))
            packet = headers + chunk(first)
            # Split in the middle of a UTF-8 character, then resume.
            split = packet.index('中'.encode()) + 1
            writer.write(packet[:split])
            await writer.drain()
            await asyncio.sleep(0.01)
            writer.write(packet[split:])
            await writer.drain()
            await asyncio.wait_for(first_seen.wait(), timeout=3)
            result['streamed_before_completion'] = True
            await asyncio.sleep(0.08)
            if mode != 'disconnect':
                writer.write(chunk(last) + b'0\r\n\r\n')
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        upstream_server = await asyncio.start_server(upstream, '127.0.0.1', 0)
    except OSError:
        journal.close(transport_complete=False)
        raise
    upstream_port = upstream_server.sockets[0].getsockname()[1]

    async def relay(client_reader, client_writer):
        remote_writer = None
        try:
            remote_reader, remote_writer = await asyncio.open_connection('127.0.0.1', upstream_port)
            async def pump(reader, writer, direction):
                while data := await reader.read(65536):
                    journal.observe(direction, data)
                    writer.write(data)
                    await writer.drain()
                if writer.can_write_eof():
                    writer.write_eof()
            await asyncio.gather(pump(client_reader, remote_writer, 'up'),
                                 pump(remote_reader, client_writer, 'down'))
        except Exception as error:
            relay_errors.append(type(error).__name__)
        finally:
            client_writer.close()
            await client_writer.wait_closed()
            if remote_writer:
                remote_writer.close()
                await remote_writer.wait_closed()
            relay_done.set()

    try:
        relay_server = await asyncio.start_server(relay, '127.0.0.1', 0)
    except OSError:
        upstream_server.close()
        await upstream_server.wait_closed()
        journal.close(transport_complete=False)
        raise
    relay_port = relay_server.sockets[0].getsockname()[1]
    began = time.monotonic_ns()
    completed = False
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', relay_port)
        writer.write(expected_request)
        await writer.drain()
        writer.write_eof()
        while data := await asyncio.wait_for(reader.read(65536), timeout=4):
            downstream_received.extend(data)
            if first in downstream_received and not first_seen.is_set():
                result['first_sse_observed_ms'] = (time.monotonic_ns() - began) / 1e6
                first_seen.set()
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(relay_done.wait(), timeout=3)
        completed = bytes(downstream_received) == expected_response and not relay_errors
    finally:
        relay_server.close()
        upstream_server.close()
        await relay_server.wait_closed()
        await upstream_server.wait_closed()
        manifest = journal.close(transport_complete=completed)
    result.update({
        'request_unchanged': bytes(upstream_received) == expected_request,
        'response_complete': completed,
        'response_bytes': len(downstream_received),
        'response_sha256': hashlib.sha256(downstream_received).hexdigest(),
        'elapsed_ms': (time.monotonic_ns() - began) / 1e6,
        'capture_complete': manifest['capture_complete'],
        'capture_issues': manifest['issues'],
        'relay_errors': relay_errors,
    })
    if mode != 'quota':
        result['capture_matches_received_bytes'] = (
            (folder / 'up.bin').read_bytes() == expected_request
            and (folder / 'down.bin').read_bytes() == bytes(downstream_received))
    assert result['request_unchanged'] and result['streamed_before_completion']
    assert completed == (mode != 'disconnect')
    assert manifest['capture_complete'] == (mode == 'normal')
    if mode != 'quota':
        assert result['capture_matches_received_bytes']
    return result


async def main(folder):
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    results = []
    for mode in ('normal', 'disconnect', 'quota'):
        results.append(await exercise(folder / mode, mode))
    report = {'synthetic': True, 'doubao_connected': False,
              'scope': 'HTTP/1.1 SSE loopback; no TLS, HTTP/2, HTTP/3 or WebSocket validation',
              'checks': results}
    (folder / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='new private directory')
    asyncio.run(main(parser.parse_args().output))
