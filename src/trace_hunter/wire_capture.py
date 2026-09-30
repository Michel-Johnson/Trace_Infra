"""Private, bounded byte journal for a future transport adapter.

This records observation time, not model/tool execution time. It does not attach
to an application, decrypt TLS, replay requests, or redact arbitrary bodies.
"""
import hashlib
import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


def _private_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb')


class WireCapture:
    def __init__(self, path, *, query_id, env_id, max_bytes=16 * 1024 * 1024,
                 queue_chunks=128):
        if not query_id or not env_id or max_bytes < 1 or queue_chunks < 1:
            raise ValueError('explicit query/env and positive limits required')
        self.path = Path(path)
        self.path.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.query_id, self.env_id = query_id, env_id
        self.max_bytes = max_bytes
        self._queue = queue.Queue(maxsize=queue_chunks)
        self._lock = threading.Lock()
        self._sequence = 0
        self._observed = {'up': 0, 'down': 0}
        self._captured = {'up': 0, 'down': 0}
        self._hash = {side: hashlib.sha256() for side in ('up', 'down')}
        self._accepted_bytes = 0
        self._dropped = 0
        self._reasons = set()
        self._closed = False
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._start = time.monotonic_ns()
        self._write_manifest('recording', False)
        self._thread = threading.Thread(target=self._writer, daemon=True)
        self._thread.start()

    def _write_manifest(self, state, transport_complete):
        record = {
            'version': 'trace-hunter/wire-probe-0.1',
            'query_id': self.query_id, 'env_id': self.env_id,
            'state': state, 'transport_complete': transport_complete,
            'capture_complete': state == 'closed' and transport_complete
                                and not self._reasons,
            'semantic_trace_complete': None,
            'observed_bytes': dict(self._observed),
            'captured_bytes': dict(self._captured),
            'captured_sha256': {k: v.hexdigest() for k, v in self._hash.items()},
            'dropped_observations': self._dropped,
            'issues': sorted(self._reasons),
            'limits': {'body_bytes': self.max_bytes,
                       'queue_chunks': self._queue.maxsize},
            'clock': 'local monotonic observation; not server or model time',
            'redaction': 'none; private local evidence, never serve publicly',
            'started_at': self._started_at,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
        temporary = self.path / 'manifest.next.json'
        with _private_file(temporary) as output:
            output.write(json.dumps(record, ensure_ascii=False, indent=2).encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.path / 'manifest.json')

    def observe(self, direction, data):
        """Nonblocking enqueue. Overload is reported, never hidden as success."""
        if direction not in self._observed or not isinstance(data, bytes):
            raise ValueError('direction must be up/down and payload must be bytes')
        if not data:
            return
        with self._lock:
            if self._closed:
                raise RuntimeError('capture is closed')
            sequence = self._sequence
            self._sequence += 1
            self._observed[direction] += len(data)
            event = {'sequence': sequence, 'direction': direction,
                     'observed_mono_ns': time.monotonic_ns() - self._start,
                     'length': len(data)}
            if self._accepted_bytes + len(data) > self.max_bytes:
                self._reasons.add('byte_limit')
                self._dropped += 1
                return
            try:
                self._queue.put_nowait((event, data))
            except queue.Full:
                self._reasons.add('queue_full')
                self._dropped += 1
            else:
                self._accepted_bytes += len(data)

    def _writer(self):
        files = {}
        try:
            for name in ('up.bin', 'down.bin', 'chunks.jsonl'):
                files[name] = _private_file(self.path / name)
            while True:
                item = self._queue.get()
                if item is None:
                    break
                event, data = item
                side = event['direction']
                event['offset'] = self._captured[side]
                files[side + '.bin'].write(data)
                self._hash[side].update(data)
                self._captured[side] += len(data)
                files['chunks.jsonl'].write(json.dumps(event).encode() + b'\n')
            for output in files.values():
                output.flush()
                os.fsync(output.fileno())
        except OSError as error:
            with self._lock:
                # An exception message can contain paths or payloads; record only type.
                self._reasons.add('writer_' + type(error).__name__)
        finally:
            for output in files.values():
                output.close()

    def close(self, *, transport_complete):
        with self._lock:
            if self._closed:
                raise RuntimeError('capture is already closed')
            self._closed = True
            if not transport_complete:
                self._reasons.add('transport_incomplete')
        # The writer can have exited after a disk error; never block forever.
        while self._thread.is_alive():
            try:
                self._queue.put(None, timeout=0.1)
                break
            except queue.Full:
                pass
        self._thread.join()
        if self._captured != self._observed:
            self._reasons.add('byte_count_mismatch')
        self._write_manifest('closed', transport_complete)
        return json.loads((self.path / 'manifest.json').read_text())
