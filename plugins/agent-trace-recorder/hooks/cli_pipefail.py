#!/usr/bin/env python3
"""Ensure Trace Hunter CLI pipelines propagate failure in the built-in Agent.

Claude's Bash tool can start a fresh shell without the worker's BASH_ENV. This
PreToolUse hook changes only Bash commands that invoke our CLI and use a pipe.
The separate Recorder capture hook remains observational.
"""

import json
import sys


def updated_input(event):
    if event.get("hook_event_name") != "PreToolUse" or event.get("tool_name") != "Bash":
        return None
    value = event.get("tool_input")
    if not isinstance(value, dict):
        return None
    command = value.get("command")
    if not isinstance(command, str) or "trace_hunter_cli.py" not in command or "|" not in command:
        return None
    if command.lstrip().startswith("set -o pipefail;"):
        return None
    return {**value, "command": "set -o pipefail; " + command}


def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, UnicodeError):
        return
    if not isinstance(event, dict):
        return
    value = updated_input(event)
    if value is not None:
        json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                          "updatedInput": value}}, sys.stdout,
                  ensure_ascii=False, separators=(",", ":"))
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
