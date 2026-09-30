"""Convert a captured export to the standard import protocol."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trace_hunter.adapters import normalize
from trace_hunter.protocol import validate

parser = argparse.ArgumentParser()
parser.add_argument('input')
parser.add_argument('--format', choices=['claude-export', 'doubao-export'], required=True)
parser.add_argument('--output', required=True)
parser.add_argument('--query-id', required=True)
parser.add_argument('--env-id', required=True)
parser.add_argument('--isolation', choices=['sandbox', 'non_sandbox', 'unknown'], default='unknown')
parser.add_argument('--network-access', choices=['allowed', 'blocked', 'unknown'], default='unknown')
args = parser.parse_args()
trace = normalize(args.input, args.format, args.query_id, args.env_id)
trace['environment'].update(isolation=args.isolation, network_access=args.network_access)
validate(trace)
Path(args.output).write_text(json.dumps(trace, ensure_ascii=False, indent=2) + '\n')
print(f"{args.output}: {len(trace['spans'])} spans")
