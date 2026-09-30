"""Owned subprocess trees stop on success, cancellation and execution failure."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src')]
from trace_hunter.processes import ProcessFailure, run_python

CHILD = r'''
import json,os,sys,time
from pathlib import Path
cfg=json.load(sys.stdin)
from subprocess import Popen
code="from pathlib import Path;import sys,time,os;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)"
child=Popen([sys.executable,'-c',code,cfg['pid_file']])
deadline=time.monotonic()+5
while not Path(cfg['pid_file']).exists():
    if time.monotonic()>deadline:raise RuntimeError('descendant startup failed')
    time.sleep(.005)
if cfg['mode']=='success':print('{}')
elif cfg['mode']=='failure':sys.exit(7)
elif cfg['mode']=='output':print('x'*10000,flush=True);time.sleep(30)
else:time.sleep(30)
'''.encode()


@unittest.skipUnless(os.name=='posix','Process group semantics require POSIX')
class ProcessTreeTests(unittest.TestCase):
    def setUp(self):
        self.folder=self.enterContext(tempfile.TemporaryDirectory())
        self.pid_file=Path(self.folder)/'descendant.pid'
        self.addCleanup(self.cleanup_descendant)

    def running(self):
        if not self.pid_file.exists():return False
        # An exited zombie is no longer executing; container PID 1 can reap later.
        value=subprocess.run(['ps','-o','stat=','-p',self.pid_file.read_text()],capture_output=True,text=True,check=False)
        return value.returncode==0 and value.stdout.strip() and not value.stdout.lstrip().startswith('Z')

    def cleanup_descendant(self):
        if self.running():
            try:os.kill(int(self.pid_file.read_text()),signal.SIGKILL)
            except ProcessLookupError:pass

    def assert_descendant_stopped(self):
        self.assertTrue(self.pid_file.exists(),'the fixture must actually start a descendant')
        deadline=time.monotonic()+2
        while self.running() and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.running(),'owned descendant survived the execution boundary')

    def execute(self,mode,**options):
        return run_python(CHILD,json.dumps({'mode':mode,'pid_file':str(self.pid_file)}).encode(),max_output_bytes=512,**options)

    def test_parent_success_does_not_leave_background_process(self):
        self.assertEqual(self.execute('success'),b'{}\n')
        self.assert_descendant_stopped()

    def test_parent_failure_does_not_leave_background_process(self):
        with self.assertRaisesRegex(ProcessFailure,'process_failed'):self.execute('failure')
        self.assert_descendant_stopped()

    def test_output_limit_stops_entire_process_group(self):
        with self.assertRaisesRegex(ProcessFailure,'output_limit'):self.execute('output')
        self.assert_descendant_stopped()

    def test_heartbeat_failure_stops_entire_process_group(self):
        def fail():
            if self.pid_file.exists():raise RuntimeError('lease expired')
        with self.assertRaisesRegex(RuntimeError,'lease expired'):self.execute('waiting',heartbeat=fail,heartbeat_seconds=.02)
        self.assert_descendant_stopped()

    def test_cancellation_stops_entire_process_group(self):
        stop=threading.Event()
        def cancel():
            if self.pid_file.exists():stop.set()
        with self.assertRaisesRegex(ProcessFailure,'stopped'):self.execute('waiting',stop=stop,heartbeat=cancel,heartbeat_seconds=.02)
        self.assert_descendant_stopped()

    def test_timeout_stops_entire_process_group(self):
        with self.assertRaisesRegex(ProcessFailure,'timeout'):self.execute('waiting',timeout_seconds=1)
        self.assert_descendant_stopped()
