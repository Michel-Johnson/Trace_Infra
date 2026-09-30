#!/usr/bin/env python3
"""Explicit project-scoped worker for installed neutral operations."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from trace_hunter.storage import Store
from trace_hunter.access import ServiceIdentities
from trace_hunter.content import LocalContentStore
from trace_hunter.workers import OfficialWorker, WorkerRegistry


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('list','register','run'))
    parser.add_argument('--project', help='Required for database commands')
    parser.add_argument('--once', action='store_true', help='Process at most one pending request')
    parser.add_argument('--invocation', help='Run one specific installed operation request')
    args = parser.parse_args(argv)
    if args.command!='run' and (args.once or args.invocation):
        parser.error('--once and --invocation apply only to run')
    if args.command!='list' and not args.project:
        parser.error('--project is required')
    registry = WorkerRegistry()
    if args.command=='list':
        print(json.dumps({'installed':[item.ref for item in registry.installed]}))
        return 0
    if not os.environ.get('DATABASE_URL'):
        parser.error('DATABASE_URL is required')
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_:stop.set())
    content_root = os.environ.get('TRACE_HUNTER_CONTENT_DIR')
    store = Store(os.environ['DATABASE_URL'], content_store=LocalContentStore(content_root) if content_root else None)
    try:
        ServiceIdentities(store.repository).get_project(args.project)
        worker = OfficialWorker(store, registry, stop=stop)
        if args.command=='register':
            operations = registry.register(worker.execution.invocations, args.project)
            print(json.dumps({'operations':[item['ref'] for item in operations]}))
            return 0
        while not stop.is_set():
            result = worker.run_once(args.project, invocation_id=args.invocation)
            if result is not None:
                # Keep invocation identity and state, not content, tokens or errors.
                print(json.dumps({key:result[key] for key in ('invocation_id','state')}), flush=True)
            if args.once or args.invocation:
                return 1 if result and result['state'] in ('failed','lease_lost') else 0
            if result is None or result['state']=='not_claimed':
                stop.wait(1)
    finally:
        store.close()
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('Invocation worker failed ('+type(error).__name__+').', file=sys.stderr)
        raise SystemExit(1)
