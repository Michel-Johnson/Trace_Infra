#!/usr/bin/env python3
"""Mirror selected Fornax Lark documents into a searchable local directory."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


DOCS = [
    {
        "slug": "trace-and-trajectory",
        "title": "[评测2.0] 什么是轨迹",
        "url": "https://bytedance.larkoffice.com/wiki/WMStwfxnQibZH1kKigLcM93lnHf",
    },
    {
        "slug": "trace-sdk-new",
        "title": "使用 Fornax SDK 上报 Trace（新版）",
        "url": "https://bytedance.larkoffice.com/wiki/Q2ExwcSDqiAt2EkVYLLczlBPnvg",
    },
    {
        "slug": "trace-span-debug-guide",
        "title": "Fornax 上报（Trace / Span）开发与 Debug 指南",
        "url": "https://bytedance.larkoffice.com/docx/AB7oda92RoDUvnxYt11cOUJdnfd",
    },
    {
        "slug": "trace-storage-es-to-bytehouse",
        "title": "从 ES 到 ByteHouse：可观测 Trace 存储升级实践",
        "url": "https://bytedance.larkoffice.com/wiki/AxBTwP2NlirifHk3tvtcRUBanRd",
    },
    {
        "slug": "trace-encryption-design",
        "title": "Fornax Trace 加密技术方案",
        "url": "https://bytedance.larkoffice.com/wiki/Fpu4wY0R4io14TkrgpAcIj9dnpW",
    },
    {
        "slug": "observability-data-unification",
        "title": "AI 应用可观测数据统一方案",
        "url": "https://bytedance.larkoffice.com/wiki/A7WewYDPxiyjXQkzPyzcA07cngw",
    },
    {
        "slug": "fornax-sdk-release-notes",
        "title": "Fornax SDK ReleaseNote",
        "url": "https://bytedance.larkoffice.com/wiki/ZjfzwcFk0ikpVLkJIqXcQH41n9g",
    },
    {
        "slug": "golang-trace-sdk",
        "title": "使用 Fornax Golang SDK 上报 Trace",
        "url": "https://bytedance.larkoffice.com/wiki/MMbnwKvczi8AShkX6BgcrQERn6b",
    },
    {
        "slug": "python-trace-sdk",
        "title": "使用 Fornax Python SDK 上报 Trace",
        "url": "https://bytedance.larkoffice.com/wiki/Cp4LwbLrYiMWS0k1222ck1hPntI",
    },
    {
        "slug": "fornax-cli-overview",
        "title": "Fornax CLI 总览",
        "url": "https://bytedance.larkoffice.com/wiki/APovwk2M1in79Fkyo1BcWZyhnGd",
    },
]


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "external" / "fornax-docs"


def fetch(url: str) -> dict:
    command = [
        "lark-cli",
        "docs",
        "+fetch",
        "--as",
        "user",
        "--doc",
        url,
        "--doc-format",
        "markdown",
        "--detail",
        "simple",
        "--format",
        "json",
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    payload = json.loads(completed.stdout)
    if payload.get("ok") is not True:
        raise RuntimeError(json.dumps(payload.get("error", payload), ensure_ascii=False))
    return payload


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    raw_dir = OUTPUT / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for spec in DOCS:
        print(f"fetching {spec['slug']}...", flush=True)
        try:
            payload = fetch(spec["url"])
            document = payload["data"]["document"]
            content = document.get("content", "")
            revision = document.get("revision_id")
            document_id = document.get("document_id")

            header = (
                f"---\n"
                f"title: {json.dumps(spec['title'], ensure_ascii=False)}\n"
                f"source: {spec['url']}\n"
                f"document_id: {document_id}\n"
                f"revision_id: {revision}\n"
                f"---\n\n"
            )
            (OUTPUT / f"{spec['slug']}.md").write_text(header + content, encoding="utf-8")
            (raw_dir / f"{spec['slug']}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            results.append({**spec, "ok": True, "revision_id": revision, "document_id": document_id})
        except Exception as exc:  # Keep the mirror useful when one document is inaccessible.
            print(f"failed {spec['slug']}: {exc}", file=sys.stderr, flush=True)
            results.append({**spec, "ok": False, "error": str(exc)})

    synced_at = datetime.now(timezone.utc).isoformat()
    manifest = {"synced_at": synced_at, "documents": results}
    (OUTPUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# Fornax Trace documentation mirror",
        "",
        f"Synced at `{synced_at}` with `scripts/sync_fornax_docs.py`.",
        "",
        "This directory is a read-only local mirror for architecture analysis. The Lark URL remains the source of truth.",
        "",
        "## Documents",
        "",
    ]
    for item in results:
        status = "ok" if item["ok"] else "failed"
        local = f"[{item['slug']}.md](./{item['slug']}.md)" if item["ok"] else item["slug"]
        lines.append(f"- `{status}` {local} — [{item['title']}]({item['url']})")
    lines.extend(
        [
            "",
            "## Refresh",
            "",
            "```bash",
            "python3 scripts/sync_fornax_docs.py",
            "```",
            "",
        ]
    )
    (OUTPUT / "README.md").write_text("\n".join(lines), encoding="utf-8")

    failures = [item for item in results if not item["ok"]]
    print(f"synced {len(results) - len(failures)}/{len(results)} documents to {OUTPUT}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
