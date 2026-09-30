"""Bind auxiliary indexes and verify bundle counts against the actual payloads."""
import hashlib
from collections import Counter
from pathlib import Path

from .corpus_import import decode, executions, normalized_execution

BUNDLE_VERSION='trace-hunter/import-bundle/1.0'


def file_entry(root, name, kind):
    raw=(Path(root)/name).read_bytes()
    return {'path':name,'kind':kind,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}


def verify_message_csv_metadata(root, manifest, auxiliary, read):
    from .import_bundle import file_digest, safe_file
    if not {'record-index.json','source-audit.json'}.issubset(auxiliary):
        raise ValueError('Message CSV bundle requires record-index.json and source-audit.json')
    records=auxiliary['record-index.json']
    if not isinstance(records,list):raise ValueError('Record index must be an array')
    by_run={record['run_id']:record for record in records}
    if len(by_run)!=len(records):
        raise ValueError('Record index contains duplicate run IDs')
    if len({record['source_row'] for record in records})!=len(records):
        raise ValueError('Record index contains duplicate source rows')
    counts=Counter();traces=set();queries=set();catalogs=[];referenced=set()
    for entry in manifest['import_files']:
        value=read(entry)
        if value.get('schema_version')=='trace-hunter/catalog/1.0':
            catalogs.append(value);continue
        run=value['run'];rid=run['id']
        if rid in traces or rid not in by_run:raise ValueError('Import files and record index disagree')
        traces.add(rid);queries.add(run['query_id']);record=by_run[rid]
        if any(record[key]!=run[key] for key in ('query_id','env_id')):
            raise ValueError('Record index identity differs from trace')
        model_spans=sum(span['kind']=='model' for span in value['spans'])
        tool_spans=sum(span['kind']=='tool' for span in value['spans'])
        if model_spans!=record['model_spans'] or tool_spans!=record['tool_spans']:
            raise ValueError('Record index span counts differ from trace')
        counts.update(runs=1,model_spans=model_spans,tool_spans=tool_spans)
        for domain,key in [('model_requests','model'),('tools','tool')]:
            state=value['coverage'][domain]
            counts[f'{key}_coverage_{state}']+=1
            if record['coverage'][domain]!=state:
                raise ValueError('Record index coverage differs from trace')
        for source in value['sources']:
            if not source['name'].startswith('raw/'):raise ValueError('Unsupported source layout')
            member=source['name'][4:];declared=manifest['raw_sources'].get(member)
            if not declared or declared['sha256']!=source['sha256']:
                raise ValueError('Raw source inventory differs from trace source')
            referenced.add(member)
    expected_runs=manifest.get('runs')
    if set(traces)!=set(by_run) or len(traces)!=expected_runs:
        raise ValueError('Manifest/index run count mismatch')
    if len(catalogs)!=1 or catalogs[0]['collection']['id']!=manifest['collection_id']:
        raise ValueError('Expected exactly the manifest collection catalog')
    if {case['query_id'] for case in catalogs[0]['cases']}!=queries:
        raise ValueError('Catalog cases do not match imported query IDs')
    if counts!=Counter(manifest['counts']):
        raise ValueError('Manifest counts differ from message CSV traces')
    profile=auxiliary['source-audit.json']
    if profile.get('errors') or profile['scope']['source_rows']!=expected_runs:
        raise ValueError('CSV field audit scope or validation status disagrees with bundle')
    if profile['scope']['conversations']!=manifest.get('conversations'):
        raise ValueError('CSV conversation count differs from manifest')
    if profile['scope']['query_groups']!=manifest.get('query_groups'):
        raise ValueError('CSV query-group count differs from manifest')
    if set(manifest['raw_sources'])!=referenced:
        raise ValueError('Unreferenced or missing raw source inventory entries')
    for member,entry in manifest['raw_sources'].items():
        path=safe_file(root,'import/raw/'+member)
        if path.stat().st_size!=entry['bytes'] or file_digest(path)!=entry['sha256']:
            raise ValueError('Raw source hash or size mismatch: '+member)
    return {'auxiliary_files_checked':len(auxiliary),'bundle_metadata_verified':True,
            'manifest_counts_checked':True,'raw_inventory_checked':True}


def verify_metadata(root, manifest):
    # Import lazily: the generic bundle verifier calls this module too.
    from .import_bundle import safe_file
    root=Path(root)
    modern=manifest.get('bundle_schema_version')==BUNDLE_VERSION
    if manifest.get('bundle_schema_version') not in (None,BUNDLE_VERSION):
        raise ValueError('Unsupported bundle_schema_version')
    def read(entry):
        raw=safe_file(root,entry['path']).read_bytes()
        if len(raw)!=entry['bytes'] or hashlib.sha256(raw).hexdigest()!=entry['sha256']:
            raise ValueError('Bundle metadata/file hash mismatch: '+entry['path'])
        return decode(raw)
    auxiliary={}
    for entry in manifest.get('auxiliary_files',[]):
        if entry['path'] in auxiliary:raise ValueError('Duplicate auxiliary file path')
        auxiliary[entry['path']]=read(entry)
    if modern and manifest.get('source_format')=='doubao-message-csv':
        return verify_message_csv_metadata(root,manifest,auxiliary,read)
    if modern and not {'session-index.json','source-audit.json'}.issubset(auxiliary):
        raise ValueError('Bundle requires hash-bound session-index.json and source-audit.json')
    if modern and manifest.get('bundle_kind')=='development_sample' and 'selection.json' not in auxiliary:
        raise ValueError('Sample requires a hash-bound selection.json')
    if not modern:
        return {'auxiliary_files_checked':len(auxiliary),'bundle_metadata_verified':False}
    sessions=auxiliary['session-index.json']
    if not isinstance(sessions,list):raise ValueError('Session index must be an array')
    by_run={s['run_id']:s for s in sessions}
    if len(by_run)!=len(sessions) or len({s['conversation_id'] for s in sessions})!=len(sessions):
        raise ValueError('Session index contains duplicate run or conversation IDs')
    counts=Counter();traces=set();queries=set();catalogs=[];referenced=set()
    for entry in manifest['import_files']:
        value=read(entry)
        if value.get('schema_version')=='trace-hunter/catalog/1.0':catalogs.append(value);continue
        run=value['run'];rid=run['id']
        if rid in traces or rid not in by_run:raise ValueError('Import files and session index disagree')
        traces.add(rid);queries.add(run['query_id']);session=by_run[rid]
        if any(session[k]!=run[k] for k in ('query_id','env_id')):
            raise ValueError('Session index identity differs from trace')
        tools=[s for s in value['spans'] if s['kind']=='tool']
        if session['executions']!=len(tools) or len(session['fragments'])!=len(value['phases']):
            raise ValueError('Session fragment/record count differs from trace')
        counts['executions']+=len(tools);counts['phases']+=len(value['phases'])
        counts['timed_executions']+=sum(s['duration_ms'] is not None or (s['start_ms'] is not None and s['end_ms'] is not None) for s in tools)
        counts['error_records']+=sum(s['status']=='error' for s in tools)
        for key in ('raw_items','duplicate_items_removed','shared_executions','shared_commands'):
            counts[key]+=session[key]
        repeats=session.get('cross_fragment_repeat_candidates',[])
        counts['cross_fragment_repeat_candidates']+=len(repeats)
        counts['cross_fragment_exact_repeats']+=sum(x['basis']=='exact_record' for x in repeats)
        counts['conversations_with_repeat_candidates']+=bool(repeats)
        ordering=session['ordering']['record_sequence_changes']
        counts['records_with_sequence_changes']+=len(ordering)
        counts['conversations_with_sequence_changes']+=bool(ordering)
        counts['phase_windows_extended']+=sum(x['reason']=='window_extended_to_observed_calls' for x in session['changes'])
        counts['timing_conflicts']+=sum(x['reason'] in ('conflicting_shared_timing','invalid_duration','inconsistent_timing') for x in session['changes'])
        for key,n in session.get('normalization',{}).items():
            if key!='error_records':counts[key]+=n
        sources={s['id']:s for s in value['sources']}
        raw_counts = Counter()
        for fragment in session['fragments']:
            if fragment['source_id'] not in sources or sources[fragment['source_id']]['name']!='raw/'+fragment['member']:
                raise ValueError('Fragment source mapping differs from trace')
            document = read({**manifest['raw_sources'][fragment['member']], 'path': 'import/raw/' + fragment['member']})
            groups, duplicates = executions(document['items'])
            raw_counts.update(raw_items=len(document['items']), duplicate_items_removed=len(duplicates),
                              executions=len(groups), shared_executions=sum(len(g)>1 for g in groups),
                              shared_commands=sum(len(g) for g in groups if len(g)>1))
            for group in groups:
                _, facts = normalized_execution(group)
                raw_counts['json_results_decoded'] += sum(f['result_encoding']=='json' for f in facts['items'])
                raw_counts['source_status_corrections'] += facts['status_corrected']
                raw_counts['explicit_business_failure_records'] += facts['explicit_business_failure']
                raw_counts['error_records'] += facts['normalized_status']=='error'
        for key in ('raw_items','duplicate_items_removed','shared_executions','shared_commands','executions'):
            if raw_counts[key] != session[key]: raise ValueError('Raw/index accounting mismatch: ' + key)
        # Old conversion versions may intentionally have different status policies.
        if value['collector']['version'] == '1.3.0':
            if any(raw_counts[k] != n for k,n in session['normalization'].items()):
                raise ValueError('Raw/index normalization accounting mismatch')
        for source in value['sources']:
            if not source['name'].startswith('raw/'):raise ValueError('Unsupported source layout')
            member=source['name'][4:];declared=manifest['raw_sources'].get(member)
            if not declared or declared['sha256']!=source['sha256']:
                raise ValueError('Raw source inventory differs from trace source')
            referenced.add(member)
    if set(traces)!=set(by_run) or len(traces)!=manifest['conversations']:
        raise ValueError('Manifest/index run count mismatch')
    if len(catalogs)!=1 or catalogs[0]['collection']['id']!=manifest['collection_id']:
        raise ValueError('Expected exactly the manifest collection catalog')
    if {c['query_id'] for c in catalogs[0]['cases']}!=queries:
        raise ValueError('Catalog cases do not match imported query IDs')
    if set(manifest['raw_sources'])!=referenced:
        raise ValueError('Unreferenced or missing raw source inventory entries')
    for member,entry in manifest['raw_sources'].items():
        read({**entry,'path':'import/raw/'+member})
    if manifest['source_fragments']!=counts['phases']:
        raise ValueError('Manifest fragment count mismatch')
    for key,n in manifest['counts'].items():
        if key not in counts or counts[key]!=n:
            raise ValueError('Manifest count mismatch: '+key)
    profile=auxiliary['source-audit.json']
    if profile['scope']['conversations'] != len(sessions): raise ValueError('Field audit conversation count mismatch')
    if profile['scope']['selected_fragments']!=counts['phases'] or profile['scope']['raw_items']!=counts['raw_items'] or profile.get('errors'):
        raise ValueError('Field audit scope or validation status disagrees with bundle')
    selection=auxiliary.get('selection.json')
    if selection is not None:
        chosen=selection['selected']
        if len(chosen)!=len(sessions) or {s['run_id'] for s in chosen}!=set(by_run):
            raise ValueError('Selection does not identify the imported sessions')
        for item in chosen:
            if item['conversation_id']!=by_run[item['run_id']]['conversation_id']:
                raise ValueError('Selected conversation identity mismatch')
    return {'auxiliary_files_checked':len(auxiliary),'bundle_metadata_verified':True,
            'manifest_counts_checked':True,'raw_inventory_checked':True}
