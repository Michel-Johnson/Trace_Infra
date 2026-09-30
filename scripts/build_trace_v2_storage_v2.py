"""Build the v2 storage profile with explicit content availability facts."""
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_trace_v2_draft import build as build_v1

VERSION = "trace-hunter/2.0-draft.2"


def build():
    schema = copy.deepcopy(build_v1())
    schema["$id"] = "urn:trace-hunter:trace:2.0-draft.2"
    schema["title"] = "Trace Hunter v2 storage profile 2 — availability-aware"
    schema["properties"]["schema_version"] = {"const": VERSION}
    identifier = {"type": "string", "minLength": 1, "maxLength": 256}
    number = {"type": "number", "minimum": 0}
    availability = {
        "type": "object",
        "properties": {
            "clock_id": identifier,
            "at_ms": number,
            "observed_at": {"type": "string", "format": "date-time"},
        },
        "additionalProperties": False,
        "oneOf": [
            {"required": ["clock_id", "at_ms"], "not": {"required": ["observed_at"]}},
            {"required": ["observed_at"],
             "not": {"anyOf": [{"required": ["clock_id"]}, {"required": ["at_ms"]}]}},
        ],
    }
    schema["$defs"]["availability"] = availability
    schema["$defs"]["content"]["properties"]["available_at"] = {
        "$ref": "#/$defs/availability"}
    schema["$defs"]["content"]["properties"].update({
        "search_text": {"type": "string", "maxLength": 100000},
        "search_text_state": {"enum": ["exact", "truncated"]},
    })
    schema["$defs"]["content"]["dependentRequired"] = {
        "search_text": ["search_text_state"], "search_text_state": ["search_text"]}
    schema["$defs"]["source"]["properties"]["available_at"] = {
        "$ref": "#/$defs/availability"}
    attributes = {
        "type": "object",
        "propertyNames": {"type": "string", "minLength": 1, "maxLength": 512},
        "additionalProperties": True,
        "maxProperties": 1000,
    }
    schema["$defs"]["attributes"] = attributes
    schema["properties"]["run"]["properties"]["attributes"] = {"$ref": "#/$defs/attributes"}
    for name in ("span", "segment", "message", "context", "tool_call"):
        schema["$defs"][name]["properties"]["attributes"] = {"$ref": "#/$defs/attributes"}
    return schema


if __name__ == "__main__":
    target = ROOT / "contracts/schemas/trace-v2-storage-v2.schema.json"
    target.write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n")
    print(target)
