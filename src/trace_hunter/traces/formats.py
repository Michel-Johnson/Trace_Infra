"""Validate source formats without rewriting their bytes or filling evidence gaps."""

import json
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker

from ..catalog import canonical
from ..contract_preview import validate as validate_v2
from ..identity import metadata
from ..interop.common import parse
from ..protocol import validate as validate_v1

SCHEMA_ROOT = Path(__file__).resolve().parents[3] / "contracts/schemas"
STORAGE_V2_SCHEMAS = {
    "trace-hunter/2.0-draft.1": json.loads((SCHEMA_ROOT / "trace-v2-storage-v1.schema.json").read_text()),
    "trace-hunter/2.0-draft.2": json.loads((SCHEMA_ROOT / "trace-v2-storage-v2.schema.json").read_text()),
}
STORAGE_V2_SCHEMA = STORAGE_V2_SCHEMAS["trace-hunter/2.0-draft.1"]
STORAGE_V2_VALIDATORS = {version: Draft202012Validator(schema, format_checker=FormatChecker())
                         for version, schema in STORAGE_V2_SCHEMAS.items()}

SUPPORTED_FORMATS = ("trace-hunter/1.0", "trace-hunter/1.1", *STORAGE_V2_SCHEMAS)


def inspect_document(raw: bytes) -> tuple[dict, dict]:
    document = parse(raw)
    version = document.get("schema_version")
    if version in SUPPORTED_FORMATS[:2]:
        validate_v1(document)
        meta = metadata(document)
        run = meta["run"]
        summary = {
            "run_id": run["id"], "query_id": run["query_id"], "env_id": run["env_id"],
            "harness": run["harness"], "model": run["model"],
            "status": run["status"], "title": run["title"],
            "environment": meta["environment"],
            "legacy_document_digest": canonical(document)[1],
            "source_document": None, "format_stability": "stable",
        }
    elif version in STORAGE_V2_VALIDATORS:
        validate_v2(document, schema_validator=STORAGE_V2_VALIDATORS[version])
        run = document["run"]
        summary = {
            "run_id": run["id"], "query_id": run.get("query_id"), "env_id": run.get("env_id"),
            "harness": run["harness"]["name"], "model": None,
            "status": run["status"], "title": run.get("title"),
            "environment": run["environment"],
            "legacy_document_digest": None,
            "source_document": document["document"], "format_stability": "experimental",
        }
    else:
        raise ValueError("Unsupported trace format")
    summary["format_version"] = version
    summary["source_verification"] = "document_bytes_only"
    # A sealed capture is not an attestation that all model inputs exist.
    return document, summary
