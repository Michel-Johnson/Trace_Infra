"""Field-by-field preflight for the supported command corpus, without model calls."""
from collections import Counter, defaultdict
from pathlib import Path

from .corpus_import import decode, epoch

ROOT = Path(__file__).resolve().parents[2]
FIELD_SPEC = decode((ROOT/'contracts/imports/doubao-command-fields-v1.json').read_bytes())


def pointer_part(value):
    return str(value).replace('~', '~0').replace('/', '~1')


def source_audit(catalog, documents):
    """documents is a mapping from archive member path to parsed JSON.

    A subset can retain the whole catalog as pointer evidence while profiling
    only its included fragments. Counts always state their scope.
    """
    sections = FIELD_SPEC['sections']
    metrics = {section:{key:{'present':0,'null':0,'empty':0,'types':Counter(), 'max_string_length':0}
                        for key in fields} for section, fields in sections.items()}
    totals, issues, unknown = Counter(), [], defaultdict(Counter)

    def issue(code, path, field, severity='warning'):
        issues.append({'code':code,'source':path,'pointer':field,'severity':severity})

    def inspect(section, value, path, pointer=''):
        totals[section] += 1
        if not isinstance(value, dict):
            issue('expected_object',path,pointer,'error'); return
        for key in value.keys() - sections[section].keys():
            if section != 'entry' or key not in ('_catalog_index', '_member', '_sha256'):
                unknown[section][key] += 1
        for key, spec in sections[section].items():
            location = pointer + '/' + pointer_part(key)
            if key not in value:
                if spec['required']: issue('missing_required_field',path,location,'error')
                continue
            v=value[key]; m=metrics[section][key]
            m['present'] += 1; m['types'][type(v).__name__] += 1
            if v is None:
                m['null'] += 1
                if not spec['nullable']: issue('null_not_allowed',path,location,'error')
                continue
            kind=spec['type']
            valid={'string':isinstance(v,str),'integer':type(v) is int,
                   'number':type(v) in (int,float),'boolean':type(v) is bool,
                   'array':isinstance(v,list),'object':isinstance(v,dict),
                   'timestamp':isinstance(v,str),'message_index':isinstance(v,str) and v.isascii() and v.isdigit()}.get(kind,False)
            if not valid: issue('expected_'+kind,path,location,'error'); continue
            if isinstance(v,str):
                m['max_string_length']=max(m['max_string_length'],len(v)); m['empty']+=v==''
            if kind=='timestamp':
                try: epoch(v)
                except (ValueError,OverflowError): issue('invalid_timestamp_with_timezone',path,location,'error')
            if kind in ('integer','number') and v<0 and key!='exitCode':
                issue('negative_'+key,path,location,'warning' if key=='durationMs' else 'error')
            if key=='sourceRow' and v==0:issue('source_row_is_one_based',path,location,'error')
            if key in ('id','conversationId','datasetId','path') and not v:
                issue('empty_identity_or_path',path,location,'error')

    catalog_path='builtin-traces/catalog.json'
    inspect('catalog',catalog,catalog_path)
    if not isinstance(catalog, dict):
        raise ValueError('Source catalog must be an object')
    datasets=catalog.get('datasets',[])
    if not isinstance(datasets,list):datasets=[]
    dataset_ids = set()
    for i,dataset in enumerate(datasets):
        inspect('dataset',dataset,catalog_path,f'/datasets/{i}')
        if isinstance(dataset, dict) and isinstance(dataset.get('id'), str):
            if dataset['id'] in dataset_ids: issue('duplicate_dataset_id',catalog_path,f'/datasets/{i}/id','error')
            dataset_ids.add(dataset['id'])
    entries=catalog.get('traces',[])
    if not isinstance(entries,list):entries=[]
    all_counts=Counter(e.get('datasetId') for e in entries if isinstance(e,dict) and isinstance(e.get('datasetId'),str))
    selected=[]
    selected_commands=Counter()
    for i,entry in enumerate(entries):
        if not isinstance(entry,dict):inspect('entry',entry,catalog_path,f'/traces/{i}');continue
        inspect('entry',entry,catalog_path,f'/traces/{i}')
        if dataset_ids and isinstance(entry.get('datasetId'), str) and entry['datasetId'] not in dataset_ids:
            issue('unknown_dataset_id',catalog_path,f'/traces/{i}/datasetId','error')
        member=str(entry.get('path','')).removeprefix('/')
        if member not in documents:continue
        selected.append(entry)
        document=documents[member];inspect('document',document,member)
        if not isinstance(document,dict):continue
        meta=document.get('meta',{});items=document.get('items',[])
        inspect('meta',meta,member,'/meta')
        if not isinstance(items,list):continue
        for j,item in enumerate(items):
            inspect('item',item,member,f'/items/{j}')
            if isinstance(item, dict):
                if item.get('status') not in (None, 'success', 'failed'):
                    issue('unknown_source_status',member,f'/items/{j}/status')
                if item.get('sharedDuration') is True and item.get('resultSeq') is None:
                    issue('shared_marker_without_result_sequence',member,f'/items/{j}/resultSeq')
        if isinstance(entry.get('datasetId'),str):selected_commands[entry['datasetId']]+=len(items)
        if not isinstance(meta,dict):continue
        observed={'commandCount':len(items),
                  'successCount':sum(isinstance(x,dict) and x.get('status')=='success' for x in items),
                  'failedCount':sum(isinstance(x,dict) and x.get('status')=='failed' for x in items)}
        for key,value in observed.items():
            if key in meta and meta[key]!=value:issue('declared_count_differs_from_raw_items',member,'/meta/'+key)
        for key in ('startedAt','duration','commandCount','successCount','failedCount'):
            if key in entry and key in meta and entry[key]!=meta[key]:
                issue('catalog_metadata_disagrees',catalog_path,f'/traces/{i}/{key}')
    dataset_summary=[]
    for i,dataset in enumerate(datasets):
        if not isinstance(dataset,dict):continue
        did=dataset.get('id') if isinstance(dataset.get('id'),str) else None; observed=all_counts[did]
        declared=dataset.get('traceCount');rows=dataset.get('rowCount')
        if declared is not None and declared!=observed:issue('dataset_trace_count_mismatch',catalog_path,f'/datasets/{i}/traceCount')
        gap=rows-observed if type(rows) is int else None
        if gap:issue('declared_rows_not_equal_exported_fragments',catalog_path,f'/datasets/{i}/rowCount')
        selected_count=sum(e.get('datasetId')==did for e in selected)
        if selected_count==observed and 'commandCount' in dataset and dataset['commandCount']!=selected_commands[did]:
            issue('dataset_command_count_mismatch',catalog_path,f'/datasets/{i}/commandCount')
        dataset_summary.append({**dataset,'id':did,'declared_rows':rows,'catalog_fragments':observed,'row_export_gap':gap,
                                'selected_fragments':selected_count, 'selected_raw_items':selected_commands[did]})
    fields=[]
    for section,specs in sections.items():
        for key,spec in specs.items():
            m=metrics[section][key]
            fields.append({'section':section,'field':key,**spec,**m,'types':dict(m['types']),
                           'missing':totals[section]-m['present'],'records_in_scope':totals[section]})
    return {'schema_version':'trace-hunter/source-audit/1.0','format':FIELD_SPEC['format'],
            'scope':{'catalog_fragments':len(entries),'selected_fragments':len(selected),
                     'conversations':len({e.get('conversationId') for e in selected if isinstance(e.get('conversationId'),str)}),
                     'raw_items':totals['item'],'subset':len(selected)!=len(entries)},
            'source_version':catalog.get('version'), 'exported_at':catalog.get('generatedAt'),'datasets':dataset_summary,'fields':fields,
            'unmapped_fields':{k:dict(v) for k,v in unknown.items()}, 'issues':issues,
            'errors':sum(x['severity']=='error' for x in issues),'warnings':sum(x['severity']=='warning' for x in issues)}


def require_valid_source(report):
    errors=[x for x in report['issues'] if x['severity']=='error']
    if errors:
        # Field paths, not captured values, make validation failures safe to share.
        raise ValueError('Source field validation failed: ' + '; '.join(
            f'{e["source"]}#{e["pointer"]}: {e["code"]}' for e in errors[:10]))
