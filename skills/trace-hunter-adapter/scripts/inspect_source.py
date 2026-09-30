#!/usr/bin/env python3
"""Print a content-free structural profile for a trace source."""
import argparse
import csv
import hashlib
import json
import tarfile
from pathlib import Path


def kind(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def shape(rows):
    fields = {}
    for row in rows:
        if not isinstance(row, dict):
            fields.setdefault("$", set()).add(kind(row))
            continue
        for key, value in row.items():
            fields.setdefault(str(key), set()).add(kind(value))
    return {key: sorted(values) for key, values in sorted(fields.items())}


def profile(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    result = {"path": str(path.resolve()), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}
    suffixes = "".join(path.suffixes).lower()
    if suffixes.endswith((".tar.gz", ".tgz", ".tar")):
        with tarfile.open(path) as archive:
            members = [member.name for member in archive.getmembers() if member.isfile()]
        result.update(format="archive", member_count=len(members), member_suffixes=sorted({"".join(Path(name).suffixes).lower() for name in members}))
        return result
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = [row for _, row in zip(range(20), reader)]
            result.update(format="csv", columns=reader.fieldnames or [], sampled_rows=len(rows), shape=shape(rows))
        return result
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
        rows = value[:20] if isinstance(value, list) else [value]
        result.update(format="json", root_type=kind(value), sampled_rows=len(rows), shape=shape(rows))
    except json.JSONDecodeError:
        rows = []
        for line in text.splitlines():
            if line.strip():
                rows.append(json.loads(line))
            if len(rows) == 20:
                break
        result.update(format="jsonl", sampled_rows=len(rows), shape=shape(rows))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    if not args.source.is_file():
        raise SystemExit("source must be a file")
    print(json.dumps(profile(args.source), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
