#!/usr/bin/env python3
"""Make a repository-maintained trace skill discoverable by local Codex."""
import argparse
import os
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--skills-dir', type=Path, default=Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'skills')
parser.add_argument(
    '--skill',
    choices=[
        'collect-session-trace', 'trace-hunter', 'trace-eval-designer', 'trace-hunter-adapter',
        'trace-deep-dive', 'trace-batch-analyzer', 'trace-analysis-reporter',
        'trace-hunter-cli',
    ],
    default='collect-session-trace',
)
args = parser.parse_args()
source = Path(__file__).resolve().parents[1] / 'skills' / args.skill
target = args.skills_dir.expanduser() / source.name
if not (source / 'SKILL.md').is_file():
    raise SystemExit('Skill source is missing.')
target.parent.mkdir(parents=True, exist_ok=True)
if target.is_symlink() and target.resolve() == source:
    print('Already installed: ' + str(target))
elif target.exists() or target.is_symlink():
    raise SystemExit('Existing skill was preserved: ' + str(target))
else:
    target.symlink_to(source, target_is_directory=True)
    print('Installed: ' + str(target))
