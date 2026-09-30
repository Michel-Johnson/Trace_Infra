"""Synthetic checks for the dedicated MulmoTerminal workspace installer."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from scripts.prepare_mulmo_workspace import prepare


def test_prepare_mulmo_workspace_preserves_existing_home() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source"
        (source / "skills" / "trace-hunter-cli").mkdir(parents=True)
        (source / "skills" / "trace-hunter-cli" / "SKILL.md").write_text("# CLI\n")
        (source / "scripts").mkdir()
        (source / "scripts" / "trace_hunter_cli.py").write_text("# synthetic CLI\n")
        (source / "scripts" / "trace_hunter_agent_prompt.txt").write_text(
            "CLI 使用 agent profile；每回合先查 capabilities。\n"
        )

        home = root / "terminal"
        workspace = prepare(home, "sample", "http://127.0.0.1:8767", source)

        assert (workspace / ".claude" / "skills" / "trace-hunter-cli" / "SKILL.md").read_text() == "# CLI\n"
        assert (workspace / "scripts" / "trace_hunter_cli.py").read_text() == "# synthetic CLI\n"
        assert "CLI 使用 terminal profile" in (workspace / "CLAUDE.md").read_text()
        config = json.loads((home / ".config" / "trace-hunter" / "config.json").read_text())
        assert config["profiles"]["terminal"] == {
            "url": "http://127.0.0.1:8767", "project": "sample"
        }
        with pytest.raises(FileExistsError):
            prepare(home, "another", "http://127.0.0.1:8767", source)
        assert "CLI 使用 terminal profile" in (workspace / "CLAUDE.md").read_text()
