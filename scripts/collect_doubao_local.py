#!/usr/bin/env python3
"""List or snapshot local Doubao Work records. Never contacts a server."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trace_hunter.local_doubao import DEFAULT_ROOT, capture, list_sessions, session_fingerprint, write_private, json_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=DEFAULT_ROOT)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('list', help='list sessions with native transcript files')
    export = sub.add_parser('export', help='snapshot a single session')
    export.add_argument('conversation_id')
    export.add_argument('--output', type=Path, required=True, help='new directory outside client data')
    export.add_argument('--query-id', required=True)
    export.add_argument('--env-id', required=True)
    export.add_argument('--query')
    export.add_argument('--model', default='未知')
    export.add_argument('--run-id', help='default: doubao-local-<conversation_id>')
    export.add_argument('--isolation', choices=['sandbox', 'non_sandbox', 'unknown'], default='unknown')
    export.add_argument('--network-access', choices=['allowed', 'blocked', 'unknown'], default='unknown')
    export.add_argument('--log-day', action='append', default=[], help='explicit SDK log day, YYYY-MM-DD')
    export.add_argument('--log-timezone', help='IANA timezone of historical log clock, e.g. Asia/Singapore')
    export.add_argument('--watch-seconds', type=float, default=0, help='bounded capture window, at most 12 hours')
    export.add_argument('--interval', type=float, default=5)
    export.add_argument('--max-snapshots', type=int, default=100)
    export.add_argument('--max-watch-mb', type=int, default=512)
    args = parser.parse_args()
    if args.action == 'list':
        print(json.dumps(list_sessions(args.data_root), ensure_ascii=False, indent=2))
        return
    from datetime import datetime
    days = [datetime.strptime(day, '%Y-%m-%d').strftime('%Y%m%d') for day in args.log_day]
    if not 0 <= args.watch_seconds <= 43200 or args.interval < 1 or args.max_snapshots < 1 or args.max_watch_mb < 1:
        parser.error('watch-seconds must be 0–43200 and interval must be at least 1')
    args.output = args.output.resolve()
    if args.output.is_relative_to(args.data_root.resolve()):
        parser.error('output must be outside the client data directory')
    options = dict(query_id=args.query_id, env_id=args.env_id, query=args.query, model=args.model,
                   run_id=args.run_id, isolation=args.isolation, network=args.network_access,
                   log_days=days, log_timezone=args.log_timezone)
    if not args.watch_seconds:
        result = capture(args.data_root, args.conversation_id, args.output, **options)
    else:
        # Each version is immutable; only the final snapshot should be imported.
        args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        deadline, sequence, previous = time.monotonic() + args.watch_seconds, 0, None
        stop_reason = 'window_ended'
        try:
            while True:
                current = session_fingerprint(args.data_root, args.conversation_id)
                if current != previous:
                    folder = args.output / ('snapshot-%05d' % sequence)
                    result = capture(args.data_root, args.conversation_id, folder,
                                     **{**options, 'log_days': []})
                    print(json.dumps({'snapshot': str(folder), 'messages': sum(a['messages'] for a in result['agents'])}), flush=True)
                    sequence += 1
                    previous = current
                    size = sum(f.stat().st_size for f in args.output.rglob('*') if f.is_file())
                    if sequence >= args.max_snapshots or size >= args.max_watch_mb * 1024 * 1024:
                        stop_reason = 'snapshot_or_storage_limit'
                        break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(args.interval, remaining))
        except KeyboardInterrupt:
            stop_reason = 'operator_stopped'
        # Scan large SDK logs once at the end, not every polling interval.
        write_private(args.output / 'watch.json', json_bytes({'state':'stopped', 'reason':stop_reason,
                     'snapshots':sequence, 'task_completed':None,
                     'note':'Quota is checked after each snapshot; final export may add one more snapshot.'}))
        args.output = args.output / 'final'
        result = capture(args.data_root, args.conversation_id, args.output, **options)
    print(json.dumps({'output': str(args.output.absolute()), 'conversation_id': args.conversation_id,
                      'messages': sum(a['messages'] for a in result['agents']),
                      'tool_calls': result['normalized_tool_calls'],
                      'tools_with_sdk_duration': result['tools_with_sdk_duration'],
                      'whole_trajectory_complete': False,
                      'log_issues': result['log_issues']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
        sys.exit(2)
