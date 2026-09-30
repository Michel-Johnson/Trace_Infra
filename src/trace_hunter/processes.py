"""Bounded execution of verified, repository-owned Python bytes.

Callers own package verification and output interpretation. This process boundary
is not an OS sandbox and must never be used to execute imported trajectory text.
"""
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time


class ProcessFailure(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def run_python(code, context, *, max_output_bytes, timeout_seconds=60,
               heartbeat=None, heartbeat_seconds=10, stop=None):
    if timeout_seconds<=0 or heartbeat_seconds<=0 or max_output_bytes<0:
        raise ValueError('Process limits must be positive')
    stopped = stop if stop is not None else threading.Event()
    if stopped.is_set():raise ProcessFailure('stopped')
    with tempfile.TemporaryDirectory(prefix='trace-worker-') as folder, tempfile.TemporaryFile() as input_file, tempfile.TemporaryFile() as output:
        path = Path(folder)/'plugin.py'
        path.write_bytes(code)
        input_file.write(context);input_file.seek(0)
        child = subprocess.Popen([sys.executable, '-I', str(path)], stdin=input_file, stdout=output,
            stderr=subprocess.DEVNULL, cwd=folder, env={'PATH':os.defpath, 'PYTHONDONTWRITEBYTECODE':'1'},
            start_new_session=os.name == 'posix')
        started = time.monotonic();next_heartbeat = started+heartbeat_seconds
        try:
            while True:
                now = time.monotonic()
                if stopped.is_set():raise ProcessFailure('stopped')
                if now-started>=timeout_seconds:raise ProcessFailure('timeout')
                if os.fstat(output.fileno()).st_size>max_output_bytes:raise ProcessFailure('output_limit')
                if child.poll() is not None:break
                if heartbeat is not None and now>=next_heartbeat:
                    heartbeat()
                    next_heartbeat = time.monotonic()+heartbeat_seconds
                stopped.wait(min(0.1, max(0, started+timeout_seconds-now)))
            if child.returncode != 0:raise ProcessFailure('process_failed')
        finally:
            if os.name == 'posix':
                # A successful parent can still leave background children. The
                # separate group belongs to this invocation, not the API/worker.
                try:os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:pass
            elif child.poll() is None:
                child.kill()
            child.wait()
        # Stop background writers before interpreting the final stdout bytes.
        output.seek(0)
        raw = output.read(max_output_bytes+1)
        if len(raw)>max_output_bytes:raise ProcessFailure('output_limit')
        return raw
