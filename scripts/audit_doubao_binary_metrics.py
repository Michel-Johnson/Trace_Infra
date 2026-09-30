#!/usr/bin/env python3
"""Inspect explicitly selected local binary logs without printing their contents."""
import argparse
import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trace_hunter.doubao_binary_metrics import audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+', type=Path)
    parser.add_argument('--output', required=True, type=Path,
                        help='new private report directory; must not exist')
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for index, path in enumerate(args.files):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 64 * 1024 * 1024:
                raise ValueError('regular source file up to 64 MiB required')
            raw = source.read(before.st_size)
            after = os.fstat(source.fileno())
        fp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        record = audit(raw)
        try:
            stable = fp(before) == fp(after) == fp(path.stat())
        except OSError:
            stable = False
        record['stable_during_read'] = stable and len(raw) == before.st_size
        record['source_filename'] = path.name
        destination = args.output / f'{index + 1:03d}.audit.json'
        with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
            json.dump(record, output, indent=2, allow_nan=False)
            output.write('\n')
        print(json.dumps({'file': path.name, 'decoded_bytes': record['decoded_bytes'],
                          'field_counts': record['field_counts'], 'report': str(destination)}))


if __name__ == '__main__':
    main()
