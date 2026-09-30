#!/usr/bin/env python3
"""Run the repository-maintained legacy bundle pipeline from any directory."""
import runpy
from pathlib import Path

repository = Path(__file__).resolve().parents[3]
entrypoint = repository / 'scripts/trace_import.py'
if not entrypoint.is_file():
    raise SystemExit('This skill needs its complete Trace Hunter repository; reinstall its repository symlink.')
runpy.run_path(str(entrypoint), run_name='__main__')
