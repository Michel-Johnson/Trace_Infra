"""Produce a compact report from the previous plugin's typed count artifact."""
import base64
import json
import sys


def main():
    context = json.load(sys.stdin)
    source = context['inputs'][0]
    if source['descriptor']['artifact_type'] != 'trace-hunter.record-counts/1':
        raise ValueError('Expected the record-count artifact type')
    counts = json.loads(base64.b64decode(source['content']['data'], validate=True))
    if counts['schema_version'] != 'trace-hunter/record-counts/1' or counts['basis'] != 'recorded_spans':
        raise ValueError('Expected recorded span counts')
    records = counts['records']
    if type(records) is not int or records < 0:
        raise ValueError('Record count must be a nonnegative integer')
    for field in ('kinds', 'statuses'):
        values = counts[field]
        if (not isinstance(values, dict) or any(type(value) is not int or value < 0 for value in values.values())
                or sum(values.values()) != records):
            raise ValueError('Count breakdown must agree with the record total')
    statuses = counts['statuses']
    ok, errors = statuses.get('ok', 0), statuses.get('error', 0)
    known = ok + errors
    report = {'schema_version': 'trace-hunter/record-report/1', 'records': counts['records'],
        'record_errors': errors, 'known_outcomes': known, 'error_fraction': errors/known if known else None,
        'basis': 'recorded_spans_with_ok_or_error_status', 'capture_coverage': counts['capture_coverage'],
        'quality_verdict': None}
    raw = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    json.dump({'schema_version': 'trace-hunter/worker-output/1', 'outputs': [{'role': 'report',
        'content': {'media_type': 'application/json', 'encoding': 'base64', 'data': base64.b64encode(raw).decode()},
        'metadata': {'basis': report['basis']}}]}, sys.stdout, ensure_ascii=False)


if __name__ == '__main__':
    main()
