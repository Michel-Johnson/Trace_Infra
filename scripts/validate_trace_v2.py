"""Compatibility CLI for the isolated contract preview; no live import is performed."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from trace_hunter.contract_preview import SCHEMA, VALIDATOR, Draft202012Validator, validate, summarize


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+")
    args = parser.parse_args()
    for filename in args.files:
        print(json.dumps(summarize(json.loads(Path(filename).read_text())), ensure_ascii=False))
