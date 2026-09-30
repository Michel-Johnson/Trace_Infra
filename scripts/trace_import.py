#!/usr/bin/env python3
"""Prepare, verify and package trace inputs without running a live import."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
# Use the repository's already installed, locked dependencies from skill callers.
# Do not install packages or use the system Python's unrelated global packages.
RUNTIME = ROOT / '.venv/bin/python'
if RUNTIME.is_file() and Path(sys.prefix).resolve() != (ROOT / '.venv').resolve():
    os.execv(str(RUNTIME), [str(RUNTIME), str(Path(__file__).resolve()), *sys.argv[1:]])
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'scripts')]
from trace_hunter.import_bundle import verify_bundle, pack_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare', help='Convert a supported source export')
    prepare.add_argument('--format', choices=['doubao-command-corpus', 'doubao-message-csv'], required=True)
    prepare.add_argument('--archive', type=Path)
    prepare.add_argument('--source', type=Path, help='Message-level CSV source')
    prepare.add_argument('--output', type=Path, required=True)
    prepare.add_argument('--context', type=Path, help='Explicit case, environment and model declarations')
    inspect = commands.add_parser('inspect', help='Inspect all source fields without converting or importing')
    inspect.add_argument('--format', choices=['doubao-command-corpus', 'doubao-message-csv'],
                         default='doubao-command-corpus')
    inspect.add_argument('--archive', type=Path)
    inspect.add_argument('--source', type=Path, help='Message-level CSV source')
    inspect.add_argument('--report', type=Path, required=True)
    sample = commands.add_parser('sample', help='Select diverse whole conversations from a prepared Doubao bundle')
    sample.add_argument('--bundle', type=Path, required=True)
    sample.add_argument('--output', type=Path, required=True)
    sample.add_argument('--count', type=int, default=50)
    sample.add_argument('--seed', default='20260911')
    sample.add_argument('--selection-from', type=Path, help='Keep the conversation set from an earlier selection.json')
    verify = commands.add_parser('verify', help='Validate files, sources, order and actual import')
    verify.add_argument('--bundle', type=Path, required=True)
    pack = commands.add_parser('pack', help='Verify and create a new self-contained archive')
    pack.add_argument('--bundle', type=Path, required=True)
    pack.add_argument('--archive', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'prepare':
        if args.format == 'doubao-command-corpus':
            if args.archive is None or args.source is not None:
                parser.error('doubao-command-corpus requires --archive and does not accept --source')
            from prepare_doubao_corpus import prepare as prepare_corpus
            result = prepare_corpus(args.archive, args.output, args.context)
        else:
            if args.source is None or args.archive is not None or args.context is not None:
                parser.error('doubao-message-csv requires --source and does not accept --archive or --context')
            from prepare_doubao_message_csv import prepare as prepare_csv
            result = prepare_csv(args.source, args.output)
        result = {'state': result['state'], 'output': str(args.output.resolve()),
                  'runs': result.get('runs', result.get('conversations')),
                  'conversations': result['conversations'], 'counts': result['counts']}
    elif args.command == 'inspect':
        from prepare_doubao_corpus import private_write, encode
        if args.format == 'doubao-command-corpus':
            if args.archive is None or args.source is not None:
                parser.error('doubao-command-corpus requires --archive and does not accept --source')
            from prepare_doubao_corpus import inspect_archive
            report = inspect_archive(args.archive)
        else:
            if args.source is None or args.archive is not None:
                parser.error('doubao-message-csv requires --source and does not accept --archive')
            from trace_hunter.doubao_message_csv import inspect_csv
            report = inspect_csv(args.source)
        private_write(args.report.resolve(), encode(report))
        result = {key: report[key] for key in ('scope', 'errors', 'warnings', 'unmapped_fields')}
        result['report'] = str(args.report.resolve())
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 1 if report['errors'] else 0
    elif args.command == 'sample':
        from trace_hunter.corpus_sample import sample_bundle
        result = sample_bundle(args.bundle, args.output, args.count, args.seed, args.selection_from)
    elif args.command == 'verify':
        result = verify_bundle(args.bundle, progress=lambda n: print(f'Verified {n}', flush=True))
    else:
        result = pack_bundle(args.bundle, args.archive)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    sys.exit(main())
