#!/usr/bin/env python3
"""Verify the dedicated terminal CLI and preload its Router Skill before Claude starts."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


START = "<!-- TRACE_HUNTER_BOOTSTRAP_START -->"
END = "<!-- TRACE_HUNTER_BOOTSTRAP_END -->"
EXPECTED_CAPABILITIES = {"trace", "span", "search", "advanced", "import", "task"}


def _cli(home: Path, workspace: Path, *args: str) -> dict:
    command = [sys.executable, str(workspace / "scripts" / "trace_hunter_cli.py"),
               "--profile", "terminal", "--compact", *args]
    env = {**os.environ, "HOME": str(home), "TRACE_HUNTER_CONFIG": str(home / ".config" / "trace-hunter" / "config.json")}
    try:
        result = subprocess.run(command, cwd=workspace, env=env, capture_output=True,
                                text=True, timeout=25, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"CLI preflight failed at {args[0]}: {type(error).__name__}") from None
    if result.returncode:
        raise RuntimeError(f"CLI preflight failed at {args[0]}: exit {result.returncode}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"CLI preflight returned invalid JSON at {args[0]}") from None
    if not isinstance(value, dict):
        raise RuntimeError(f"CLI preflight returned non-object at {args[0]}")
    return value


def preflight(home: Path, identity_template: Path) -> dict:
    home = home.resolve()
    workspace = home / "workspace"
    router_file = workspace / ".claude" / "skills" / "trace-hunter" / "SKILL.md"
    config = json.loads((home / ".config" / "trace-hunter" / "config.json").read_text(encoding="utf-8"))
    terminal = config.get("profiles", {}).get("terminal", {})
    project = terminal.get("project")
    if not isinstance(project, str) or not project:
        raise RuntimeError("terminal profile has no default project")
    router = router_file.read_text(encoding="utf-8")
    identity = identity_template.read_text(encoding="utf-8").replace("CLI 使用 agent profile", "CLI 使用 terminal profile")

    capabilities = _cli(home, workspace, "capabilities")
    if not EXPECTED_CAPABILITIES.issubset(capabilities):
        raise RuntimeError("CLI capabilities response lacks required families")
    project_info = _cli(home, workspace, "project", "get", project)
    if project_info.get("project_id") != project:
        raise RuntimeError("CLI project lookup returned a different project")
    sample = _cli(home, workspace, "trace", "query", "--limit", "1")
    if not isinstance(sample.get("items"), list):
        raise RuntimeError("CLI trace query did not return an items list")

    record = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "project_id": project,
        "router_sha256": hashlib.sha256(router.encode("utf-8")).hexdigest(),
        "cli_checks": ["capabilities", "project.get", "trace.query.limit_1"],
        "capability_families": sorted(EXPECTED_CAPABILITIES),
        "sample_count": len(sample["items"]),
    }
    bootstrap = (
        f"\n{START}\n"
        "以下 Router Skill 在 Claude 启动前已载入本项目上下文；按用户目标直接选专项 Skill，"
        "不必为找路由重复读取它。不要把预检当成后续接口永远可用的证明。\n\n"
        f"## CLI 启动前只读预检\n"
        f"UTC 时间：{record['verified_at']}；项目：{project}；"
        "已通过 capabilities、project get、trace query --limit 1。"
        "CLI 使用 terminal profile；若服务能力变化或调用出错，重新查询 capabilities。"
        "预检不执行导入、评测或分析。\n\n"
        f"## Trace Hunter Router Skill（SHA-256: {record['router_sha256']}）\n\n"
        f"{router.rstrip()}\n{END}\n"
    )
    prompt = identity.rstrip() + "\n" + bootstrap
    pending = workspace / ".CLAUDE.md.preflight-pending"
    pending.write_text(prompt, encoding="utf-8")
    pending.chmod(0o600)
    pending.replace(workspace / "CLAUDE.md")
    state_dir = workspace / ".trace-hunter"
    state_dir.mkdir(mode=0o700, exist_ok=True)
    state = state_dir / ".preflight.json.pending"
    state.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    state.replace(state_dir / "preflight.json")
    return record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--identity-template", required=True, type=Path)
    args = parser.parse_args()
    record = preflight(args.home, args.identity_template)
    print(f"Trace Hunter Agent preflight passed: {record['project_id']} ({', '.join(record['cli_checks'])})")


if __name__ == "__main__":
    main()
