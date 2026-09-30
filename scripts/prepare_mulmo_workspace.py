#!/usr/bin/env python3
"""Install the repository's CLI and Skills into a dedicated MulmoTerminal home."""

import argparse
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def prepare(home: Path, project: str, api_url: str, source_root: Path = ROOT) -> Path:
    home = home.resolve()
    source_root = source_root.resolve()
    workspace = home / "workspace"
    skills_root = workspace / ".claude" / "skills"
    if workspace.exists():
        raise FileExistsError(f"Refusing to overwrite existing terminal workspace: {workspace}")
    skills_root.mkdir(parents=True, mode=0o700)
    for skill in (source_root / "skills").iterdir():
        if (skill / "SKILL.md").is_file():
            shutil.copytree(skill, skills_root / skill.name)

    cli = source_root / "scripts" / "trace_hunter_cli.py"
    for destination in (workspace / "scripts" / "trace_hunter_cli.py",
                        skills_root / "trace-hunter-cli" / "scripts" / "trace_hunter_cli.py"):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cli, destination)

    identity = (source_root / "scripts" / "trace_hunter_agent_prompt.txt").read_text(
        encoding="utf-8"
    )
    identity = identity.replace("CLI 使用 agent profile", "CLI 使用 terminal profile")
    (workspace / "CLAUDE.md").write_text(identity, encoding="utf-8")
    service = skills_root / "trace-hunter-cli" / "references" / "service.json"
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text(json.dumps({"name": "terminal", "url": api_url,
                                   "api_contract": api_url + "/api/openapi.json",
                                   "authentication": "none", "default_project": project}), encoding="utf-8")
    config = home / ".config" / "trace-hunter" / "config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"version": 1, "current": "terminal", "profiles": {
        "terminal": {"url": api_url, "project": project}}}), encoding="utf-8")
    config.chmod(0o600)
    return workspace


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--project", default="benchmark-a")
    parser.add_argument("--api-url", default="http://127.0.0.1:8767")
    parser.add_argument("--source-root", type=Path, default=ROOT)
    args = parser.parse_args()
    print(prepare(args.home, args.project, args.api_url, args.source_root))


if __name__ == "__main__":
    main()
