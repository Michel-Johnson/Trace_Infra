"""Deterministic Skill bundle identities shared by API and built-in Agent worker."""

import hashlib
import json
from pathlib import Path


def skill_files(skill: Path, skills_directory: Path, cli_path: Path):
    files = {}
    for path in sorted(skill.rglob('*')):
        relative = path.relative_to(skills_directory)
        if not path.is_file() or path.is_symlink() or any(part.startswith('.') for part in relative.parts):
            continue
        files[relative.as_posix()] = path.read_bytes()
    if skill.name == 'trace-hunter-cli':
        files['trace-hunter-cli/scripts/trace_hunter_cli.py'] = cli_path.read_bytes()
    return files


def content_digest(files) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        encoded = name.encode()
        digest.update(len(encoded).to_bytes(4, 'big'))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, 'big'))
        digest.update(content)
    return digest.hexdigest()


def build_skill_manifest(skills_directory: Path, cli_path: Path) -> dict:
    skills = sorted(path for path in skills_directory.iterdir()
                    if path.is_dir() and not path.is_symlink() and (path / 'SKILL.md').is_file())
    if not skills or not cli_path.is_file():
        raise RuntimeError('可下载的 Skill 或 CLI 不完整')
    bundle = json.loads((skills_directory / 'bundle.json').read_text())
    service = json.loads((skills_directory / 'trace-hunter-cli' / 'references' / 'service.json').read_text())
    return {
        'format': 'trace-hunter-skill-manifest/1',
        'bundle_version': bundle['bundle_version'],
        'check_url': bundle['check_url'],
        'download_url': bundle['download_url'],
        'page_url': bundle['page_url'],
        'skills': [
            {'name': path.name, 'sha256': content_digest(skill_files(path, skills_directory, cli_path))}
            for path in skills
        ],
        'cli': 'skills/trace-hunter-cli/scripts/trace_hunter_cli.py',
        'service': service,
    }
