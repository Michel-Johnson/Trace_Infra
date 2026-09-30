"""Count recorded spans only. Never execute text found in a trajectory."""
import base64
from collections import Counter
import json
import sys


def main():
    context = json.load(sys.stdin)
    source = context['inputs'][0]
    document = json.loads(base64.b64decode(source['content']['data'], validate=True))
    if document['schema_version'] not in ('trace-hunter/1.0', 'trace-hunter/1.1', 'trace-hunter/2.0-draft.1'):
        raise ValueError('Unsupported trace format')
    spans = document['spans']
    counts = {'schema_version': 'trace-hunter/record-counts/1', 'basis': 'recorded_spans',
        'records': len(spans), 'kinds': dict(sorted(Counter(item['kind'] for item in spans).items())),
        'statuses': dict(sorted(Counter(item['status'] for item in spans).items())),
        'capture_coverage': document.get('coverage') if document['schema_version']!='trace-hunter/2.0-draft.1' else document.get('capture', {}).get('coverage'),
        'quality_verdict': None}
    raw = json.dumps(counts, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    json.dump({'schema_version': 'trace-hunter/worker-output/1', 'outputs': [{'role': 'counts',
        'content': {'media_type': 'application/json', 'encoding': 'base64', 'data': base64.b64encode(raw).decode()},
        'metadata': {'basis': 'recorded_spans'}}]}, sys.stdout, ensure_ascii=False)


if __name__ == '__main__':
    main()
