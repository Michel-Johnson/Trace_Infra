#!/usr/bin/env python3
"""Read native Doubao network logs and export timing-only evidence locally."""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trace_hunter.doubao_network_metrics import audit
from capture_doubao_cdp import private_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('logs', type=Path, nargs='+')
    parser.add_argument('--start-unix-s', type=float)
    parser.add_argument('--end-unix-s', type=float)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output file must not exist')
    if args.start_unix_s is not None and args.end_unix_s is not None and args.start_unix_s > args.end_unix_s:
        parser.error('Start must precede end')
    result = audit(args.logs, args.start_unix_s, args.end_unix_s)
    args.output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    # Reserve exclusively before the shared private writer, so prior evidence stays intact.
    os.close(os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    private_json(args.output, result)
    print(f"Saved {len(result['requests'])} timing records; {result['decode_errors']} malformed request logs.")


if __name__ == '__main__':
    main()
