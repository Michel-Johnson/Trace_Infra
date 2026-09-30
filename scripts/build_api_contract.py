"""Build the core Trace HTTP contract without opening a database."""

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "apps/api")]


def _embedded(schema, name):
    def rewrite(value):
        if isinstance(value, list): return [rewrite(item) for item in value]
        if isinstance(value, dict):
            return {key: "#/components/schemas/" + name + item[1:]
                    if key == "$ref" and isinstance(item, str) and item.startswith("#/")
                    else rewrite(item) for key, item in value.items() if key not in ("$id", "$schema")}
        return value
    return rewrite(schema)


def make_spec():
    from trace_hunter_api.contract import trace_revision_contract
    contract = trace_revision_contract()
    contract["info"] = {
        "title": "Trace Hunter Core API", "version": "4.0.0",
        "description": "不可变 Trace revision、自动 Agent 识别与导入、可恢复上传、Recorder 来源回读、OTLP、固定快照查询、Metrics 与 literal/regex 搜索。",
    }
    schemas = contract.setdefault("components", {}).setdefault("schemas", {})
    v11 = _embedded(json.loads((ROOT / "schemas/trace-v1.schema.json").read_text()), "TraceV11")
    v10 = _embedded(json.loads((ROOT / "schemas/trace-v1.0.schema.json").read_text()), "TraceV10")
    v2 = _embedded(json.loads((ROOT / "contracts/schemas/trace-v2-storage-v2.schema.json").read_text()), "TraceV2Draft")
    schemas.update(TraceV11=v11, TraceV10=v10, TraceV2Draft=v2,
                   TraceInput={"oneOf": [{"$ref": "#/components/schemas/TraceV11"},
                                         {"$ref": "#/components/schemas/TraceV10"}]})
    return contract


def synthetic_inputs():
    first = json.loads((ROOT / "examples/minimal.trace.json").read_text())
    second = copy.deepcopy(first)
    second["run"].update(id="example-ledger-alternate", harness="Alternate harness",
                         model="Alternate model", env_id="alternate-env-v1")
    catalog = {"schema_version": "trace-hunter/catalog/1.0",
               "collection": {"id": "removed", "title": "Removed", "kind": "evaluation", "description": ""},
               "cases": []}
    return first, second, catalog


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def main():
    write_json(ROOT / "contracts/openapi.json", make_spec())
    print("Built the Trace Hunter core OpenAPI contract.")


if __name__ == "__main__":
    main()
