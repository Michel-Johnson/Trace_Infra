#!/usr/bin/env python3
"""Repository-owned worker entrypoint; DATABASE_URL is supplied by deployment."""
import os
import signal
import sys
import threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from trace_hunter.storage import Store
from trace_hunter.evaluations.service import Evaluations
from trace_hunter.evaluations.worker import run_once
from trace_hunter.extensions.service import Extensions
from trace_hunter.extensions.worker import run_once as run_contribution

def main():
    stop=threading.Event()
    for signum in (signal.SIGINT,signal.SIGTERM):signal.signal(signum,lambda *_:stop.set())
    store=Store(os.environ['DATABASE_URL']);service=Evaluations(store);extensions=Extensions(store,service)
    try:
        service.bootstrap()
        extensions.bootstrap()
        while not stop.is_set():
            evaluated=run_once(service);classified=run_contribution(extensions.tasks)
            if not evaluated and not classified:stop.wait(1)
    finally:store.close()

if __name__=='__main__':main()
