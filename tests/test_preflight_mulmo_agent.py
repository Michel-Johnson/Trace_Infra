"""Synthetic preflight checks; never contact the real Trace Hunter API."""

import json
import subprocess
from pathlib import Path

import pytest

from scripts.preflight_mulmo_agent import EXPECTED_CAPABILITIES, preflight


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "terminal"
    workspace = home / "workspace"
    router = workspace / ".claude" / "skills" / "trace-hunter" / "SKILL.md"
    router.parent.mkdir(parents=True)
    router.write_text("# Router\nUse the CLI.\n")
    (workspace / "scripts").mkdir()
    (workspace / "scripts" / "trace_hunter_cli.py").write_text("# synthetic\n")
    (workspace / "CLAUDE.md").write_text("Original prompt\n")
    config = home / ".config" / "trace-hunter" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"profiles": {"terminal": {"project": "sample", "url": "http://127.0.0.1:8767"}}}))
    identity = tmp_path / "identity.txt"
    identity.write_text("Trace Hunter Agent. CLI 使用 agent profile。\n")
    return home, identity


def test_preflight_preloads_router_after_read_only_cli_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, identity = _workspace(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[-1] == "capabilities":
            value = {name: {} for name in EXPECTED_CAPABILITIES}
        elif command[-3:] == ["project", "get", "sample"]:
            value = {"project_id": "sample"}
        else:
            assert command[-4:] == ["trace", "query", "--limit", "1"]
            value = {"items": [{"run_id": "synthetic"}]}
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    record = preflight(home, identity)
    assert len(calls) == 3
    assert record["sample_count"] == 1
    prompt = (home / "workspace" / "CLAUDE.md").read_text()
    assert "CLI 使用 terminal profile" in prompt
    assert "# Router\nUse the CLI." in prompt
    assert "预检不执行导入、评测或分析" in prompt
    assert json.loads((home / "workspace" / ".trace-hunter" / "preflight.json").read_text())["project_id"] == "sample"


def test_failed_preflight_preserves_previous_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, identity = _workspace(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 2, "", "error"))
    with pytest.raises(RuntimeError, match="capabilities"):
        preflight(home, identity)
    assert (home / "workspace" / "CLAUDE.md").read_text() == "Original prompt\n"


def test_preflight_delivers_execution_task_confirmation_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _ = _workspace(tmp_path)
    identity = Path(__file__).resolve().parents[1] / "scripts" / "trace_hunter_agent_prompt.txt"

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[-1] == "capabilities":
            value = {name: {} for name in EXPECTED_CAPABILITIES}
        elif command[-3:] == ["project", "get", "sample"]:
            value = {"project_id": "sample"}
        else:
            value = {"items": []}
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    preflight(home, identity)
    prompt = (home / "workspace" / "CLAUDE.md").read_text()
    assert "Eval Spec 设计" in prompt
    assert "是否创建这个任务并开始执行" in prompt
    assert "未获肯定答复前不创建任务" in prompt
    assert "task create" in prompt and "task update" in prompt
    assert "UUIDv4" in prompt
    assert "所有 CLI 调用都带 `--profile terminal`" in prompt
    assert "./.claude/skills/trace-hunter-cli/SKILL.md" in prompt
    assert '"steps":[{"id":"prepare"' in prompt
    assert '"state":"succeeded"' in prompt
    assert "回读任务确认终态" in prompt
