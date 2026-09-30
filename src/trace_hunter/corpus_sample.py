"""Deterministic development samples of whole observed Doubao conversations."""
from collections import Counter
from pathlib import Path
import re

from .corpus_import import decode, digest
from .import_bundle import safe_file, verify_imports

VERSION = 'doubao-diverse-conversations/1'
SIZE_BINS = [('1-5', 5, 7), ('6-15', 15, 10), ('16-35', 35, 12),
             ('36-100', 100, 13), ('101+', float('inf'), 8)]


def encoded(value):
    import json
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()


def write_new(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('xb') as output:
        path.chmod(0o600)
        output.write(raw)


def checked_raw(root, entry):
    raw = safe_file(root, entry['path']).read_bytes()
    if len(raw) != entry['bytes'] or digest(raw) != entry['sha256']:
        raise ValueError('Source bundle file hash or size mismatch: ' + entry['path'])
    return raw


def operation_families(operation):
    """Labels describe observed CLI operations, never inferred model tool kinds."""
    if operation == '--help':
        return {'help'}
    if not re.fullmatch(r'\+[a-z]+(?:-[a-z]+)*', operation):
        return {'unknown'}
    prefix = operation[1:].split('-')[0]
    family = {'role': 'permissions', 'advperm': 'permissions', 'data': 'query',
              'url': 'query', 'title': 'query', 'search': 'query'}.get(prefix, prefix)
    if family not in {'base', 'table', 'field', 'record', 'view', 'form', 'dashboard',
                      'workflow', 'permissions', 'app', 'workspace', 'template', 'button', 'query'}:
        family = 'unknown'
    return {family, 'attachment'} if 'attachment' in operation else {family}


def profile_bundle(root):
    root = Path(root).resolve()
    manifest_raw = safe_file(root, 'manifest.json').read_bytes()
    manifest = decode(manifest_raw)
    if manifest.get('state') != 'ready' or (root / 'INCOMPLETE').exists():
        raise ValueError('Only a ready prepared bundle can be sampled')
    from .bundle_integrity import verify_metadata
    verify_metadata(root, manifest)
    sessions_raw = safe_file(root, 'session-index.json').read_bytes()
    sessions = decode(sessions_raw)
    files = {f['run_id']: f for f in manifest['import_files'] if 'run_id' in f}
    ids = [s['run_id'] for s in sessions]
    conversations = [s['conversation_id'] for s in sessions]
    if (len(files) != len(ids) or len(set(ids)) != len(ids) or set(ids) != set(files)
            or len(set(conversations)) != len(ids) or len(ids) != manifest['conversations']):
        raise ValueError('Session index must map every run and conversation exactly once')
    profiles = []
    for session in sessions:
        entry = files[session['run_id']]
        trace = decode(checked_raw(root, entry))
        spans = [s for s in trace['spans'] if s['kind'] == 'tool']
        count = len(spans)
        if not count or count != session['executions'] or trace['run']['id'] != session['run_id']:
            raise ValueError('Unsupported or inconsistent source session')
        size_bin = next(name for name, limit, _ in SIZE_BINS if count <= limit)
        timed = sum(s['duration_ms'] is not None or (s['start_ms'] is not None and s['end_ms'] is not None) for s in spans)
        timing = 'none' if not timed else 'all' if timed == count else 'partial'
        failures = sum(s['status'] == 'error' for s in spans)
        status = 'none' if not failures else 'all' if failures == count else 'some'
        fragments = session['fragments']
        source_names = {s['name'] for s in trace['sources']}
        operations = set()
        for fragment in fragments:
            member = fragment['member']
            if 'raw/' + member not in source_names:
                raise ValueError('Session fragment missing from trace sources')
            source_entry = {**manifest['raw_sources'][member], 'path': 'import/raw/' + member}
            document = decode(checked_raw(root, source_entry))
            operations.update(str(item.get('operation', 'unknown')) for item in document['items'])
        families = set().union(*(operation_families(op) for op in operations))
        datasets = sorted({f['datasetId'] for f in fragments})
        fragment_bin = '1' if len(fragments) == 1 else '2-3' if len(fragments) <= 3 else '4-7' if len(fragments) <= 7 else '8+'
        changes = Counter(c['reason'] for c in session['changes'])
        flags = {
            'multiple_fragments': len(fragments) > 1,
            'multiple_datasets': len(datasets) > 1,
            'single_late_fragment': len(fragments) == 1 and int(fragments[0]['messageIndex']) > 1,
            'within_fragment_duplicates': bool(session['duplicate_items_removed']),
            'shared_execution': bool(session['shared_executions']),
            'cross_fragment_repeats': bool(session['cross_fragment_repeat_candidates']),
            'reordered_records': bool(session['ordering']['record_sequence_changes']),
            'extended_phase_window': bool(changes['window_extended_to_observed_calls']),
        }
        features = {f'size:{size_bin}', f'timing:{timing}', f'failures:{status}', f'fragments:{fragment_bin}'}
        features.update('flag:' + k for k, enabled in flags.items() if enabled)
        features.update('dataset:' + d for d in datasets)
        features.update('family:' + f for f in families)
        features.update('operation:' + op for op in operations)
        profiles.append({'conversation_id': session['conversation_id'], 'run_id': session['run_id'],
                         'query_id': session['query_id'], 'env_id': session['env_id'],
                         'records': count, 'trace_bytes': entry['bytes'], 'size_bin': size_bin,
                         'fragments': len(fragments), 'datasets': datasets,
                         'timed_records': timed, 'timing': timing, 'failed_records': failures,
                         'families': sorted(families), 'operations': sorted(operations),
                         'flags': flags, 'features': sorted(features)})
    return manifest, sessions, profiles, digest(manifest_raw), digest(sessions_raw)


def size_quotas(profiles, count):
    available = Counter(p['size_bin'] for p in profiles)
    quotas = {name: 0 for name, _, _ in SIZE_BINS}
    # Weighted round-robin gives 7/10/12/13/8 at n=50; redistributes empty strata.
    for _ in range(count):
        eligible = [(quotas[name] / weight, index, name) for index, (name, _, weight) in enumerate(SIZE_BINS)
                    if quotas[name] < available[name]]
        if not eligible:
            raise ValueError('Sample count exceeds available conversations')
        quotas[min(eligible)[2]] += 1
    return quotas


def select_diverse(profiles, count=50, seed='20260911'):
    if not isinstance(count, int) or count < 1 or count > len(profiles):
        raise ValueError('Sample count must be between 1 and the number of conversations')
    if len({p['run_id'] for p in profiles}) != len(profiles):
        raise ValueError('Duplicate profile run ID')
    quotas = size_quotas(profiles, count)
    available = Counter(f for p in profiles for f in p['features'])
    requested = {'timing:none': 6, 'timing:all': 10, 'timing:partial': 10,
                 'failures:none': 12, 'failures:some': 15, 'failures:all': 1,
                 'fragments:1': 15, 'fragments:2-3': 10, 'fragments:4-7': 6, 'fragments:8+': 3,
                 'flag:multiple_fragments': 20, 'flag:multiple_datasets': 8,
                 'flag:single_late_fragment': 4, 'flag:within_fragment_duplicates': 5,
                 'flag:shared_execution': 4, 'flag:cross_fragment_repeats': 10,
                 'flag:reordered_records': 5, 'flag:extended_phase_window': 4}
    for feature in available:
        if feature.startswith('dataset:'): requested[feature] = 8
        elif feature.startswith('family:'): requested[feature] = 2
        elif feature.startswith('operation:'): requested[feature] = 1
    targets = {f: min(available[f], max(1, round(n * count / 50))) for f, n in requested.items() if available[f]}
    counts, bin_counts, selected, decisions = Counter(), Counter(), [], {}
    tie = lambda p: digest((str(seed) + '\0' + p['run_id']).encode())

    def add(profile, reasons):
        selected.append(profile)
        decisions[profile['run_id']] = {'selection_rank': len(selected), 'reasons': sorted(reasons)}
        counts.update(profile['features']); bin_counts[profile['size_bin']] += 1

    # Explicit observable extremes are useful regression/performance fixtures.
    for metric, largest in [('records', False), ('records', True), ('fragments', True), ('trace_bytes', True)]:
        candidates = sorted(profiles, key=lambda p: ((-1 if largest else 1) * p[metric], tie(p)))
        candidate = candidates[0]
        reason = ('maximum:' if largest else 'minimum:') + metric
        if candidate['run_id'] in decisions:
            decisions[candidate['run_id']]['reasons'].append(reason)
        elif len(selected) < count and bin_counts[candidate['size_bin']] < quotas[candidate['size_bin']]:
            add(candidate, [reason])
    while len(selected) < count:
        candidates = [p for p in profiles if p['run_id'] not in decisions and bin_counts[p['size_bin']] < quotas[p['size_bin']]]
        def gain(profile):
            total = 0.0
            for feature in profile['features']:
                target = targets.get(feature, 0)
                if counts[feature] >= target: continue
                weight = 1 if feature.startswith('operation:') else 8
                total += weight * (target - counts[feature]) / target / (available[feature] ** 0.5)
            return total
        candidate = min(candidates, key=lambda p: (-gain(p), tie(p)))
        reasons = [f for f in candidate['features'] if counts[f] < targets.get(f, 0)]
        add(candidate, reasons or ['size:' + candidate['size_bin']])
    unmet = {f: {'target': n, 'actual': counts[f]} for f, n in sorted(targets.items()) if counts[f] < n}
    # Restore corpus chronological order. Selection order remains in the audit.
    chosen = [{**p, **decisions[p['run_id']]} for p in profiles if p['run_id'] in decisions]
    return chosen, {'algorithm': VERSION, 'seed': str(seed), 'count': count, 'unit': 'conversation_id',
                    'size_quotas': quotas, 'population_size': len(profiles),
                    'population_features': dict(sorted(available.items())),
                    'sample_features': dict(sorted(counts.items())), 'coverage_targets': targets,
                    'unmet_targets': unmet,
                    'purpose': 'Development coverage, not an unbiased estimate of production quality.',
                    'session_policy': 'Preserve every available fragment in each selected conversation; original session completeness is unknown.'}


def retained_selection(profiles, previous, count):
    raw = Path(previous).read_bytes()
    prior = decode(raw)
    selected = prior['selected']
    old = {p['conversation_id']:p for p in selected}
    if len(old) != len(selected) or len(old) != count:
        raise ValueError('Previous selection must contain exactly count distinct conversations')
    current = {p['conversation_id']:p for p in profiles}
    if not set(old).issubset(current):
        raise ValueError('A previously selected conversation is missing from this corpus')
    chosen = [{**p, 'selection_rank':old[p['conversation_id']]['selection_rank'],
               'reasons':['retained_from_previous_selection'],
               'previous_run_id':old[p['conversation_id']]['run_id'],
               'previous_reasons':old[p['conversation_id']]['reasons']}
              for p in profiles if p['conversation_id'] in old]
    available = Counter(f for p in profiles for f in p['features'])
    counts = Counter(f for p in chosen for f in p['features'])
    targets = prior.get('coverage_targets', {})
    return chosen, {'algorithm':'retain-conversations/1', 'seed':prior.get('seed'), 'count':count,
                    'unit':'conversation_id', 'population_size':len(profiles),
                    'size_quotas':{name:sum(p['size_bin']==name for p in chosen) for name,_,_ in SIZE_BINS},
                    'population_features':dict(sorted(available.items())), 'sample_features':dict(sorted(counts.items())),
                    'coverage_targets':targets,
                    'unmet_targets':{f:{'target':n,'actual':counts[f]} for f,n in targets.items() if counts[f]<n},
                    'previous_selection_sha256':digest(raw),
                    'purpose':'Retain the exact conversation set across normalization revisions; features are recomputed.',
                    'session_policy':'Preserve every available fragment; original session completeness is unknown.'}


def sample_bundle(bundle, output, count=50, seed='20260911', selection_from=None):
    root, output = Path(bundle).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(root):
        raise ValueError('Use a new output directory outside the source bundle')
    manifest, sessions, profiles, manifest_sha, index_sha = profile_bundle(root)
    chosen, selection = retained_selection(profiles, selection_from, count) if selection_from else select_diverse(profiles, count, seed)
    selected_ids = {p['run_id'] for p in chosen}
    selected_sessions = [s for s in sessions if s['run_id'] in selected_ids]
    selection.update(source_manifest_sha256=manifest_sha, source_session_index_sha256=index_sha,
                     selected=chosen)
    sample_id = digest(encoded({'source': manifest_sha, 'algorithm': selection['algorithm'], 'seed': selection['seed'],
                               'runs': [p['run_id'] for p in chosen]}))[:16]
    output.mkdir(parents=True, mode=0o700)
    write_new(output / 'INCOMPLETE', b'Sampling or verification has not finished.\n')
    catalog_entries = [f for f in manifest['import_files'] if f.get('kind') == 'catalog']
    if len(catalog_entries) != 1:
        raise ValueError('Expected one source collection catalog')
    catalog = decode(checked_raw(root, catalog_entries[0]))
    catalog['collection'].update(id='doubao-dev-sample-' + sample_id,
                                title=f'豆包工作 · 多样化开发样本 {count}',
                                description='按会话完整保留已有片段的开发覆盖样本；不是独立留出评测集。')
    queries = {p['query_id'] for p in chosen}
    catalog['cases'] = [c for c in catalog['cases'] if c['query_id'] in queries]
    if {c['query_id'] for c in catalog['cases']} != queries:
        raise ValueError('Selected cases missing from source catalog')
    catalog_raw = encoded(catalog)
    write_new(output / 'import/catalog.json', catalog_raw)
    files = [{'path': 'import/catalog.json', 'bytes': len(catalog_raw),
              'sha256': digest(catalog_raw), 'kind': 'catalog'}]
    copied_sources, totals = {}, Counter()
    for entry in manifest['import_files']:
        if entry.get('run_id') not in selected_ids: continue
        raw = checked_raw(root, entry)
        write_new(output / entry['path'], raw)
        files.append(entry)
        trace = decode(raw)
        totals['timed_executions'] += sum(s['duration_ms'] is not None or (s['start_ms'] is not None and s['end_ms'] is not None) for s in trace['spans'])
        for source in trace['sources']:
            name = source['name']
            member = name.removeprefix('raw/')
            if not name.startswith('raw/') or member not in manifest['raw_sources']:
                raise ValueError('Unsupported raw source layout')
            if member in copied_sources: continue
            raw_source = safe_file((root / entry['path']).parent, name).read_bytes()
            source_meta = manifest['raw_sources'][member]
            if (digest(raw_source) != source['sha256'] or digest(raw_source) != source_meta['sha256']
                    or len(raw_source) != source_meta['bytes']):
                raise ValueError('Raw source hash or size mismatch')
            # Keep the whole source catalog: existing JSON pointers use its original indexes.
            write_new(output / Path(entry['path']).parent / name, raw_source)
            copied_sources[member] = source_meta
    for session in selected_sessions:
        totals.update(session.get('normalization', {}))
        for key in ('raw_items', 'duplicate_items_removed', 'shared_executions', 'shared_commands', 'executions'):
            totals[key] += session[key]
        totals['phases'] += len(session['fragments'])
        totals['phase_windows_extended'] += sum(c['reason'] == 'window_extended_to_observed_calls' for c in session['changes'])
        totals['timing_conflicts'] += sum(c['reason'] in ('conflicting_shared_timing', 'invalid_duration', 'inconsistent_timing') for c in session['changes'])
        repeats = session['cross_fragment_repeat_candidates']
        totals['cross_fragment_repeat_candidates'] += len(repeats)
        totals['cross_fragment_exact_repeats'] += sum(r['basis'] == 'exact_record' for r in repeats)
        totals['conversations_with_repeat_candidates'] += bool(repeats)
        totals['records_with_sequence_changes'] += len(session['ordering']['record_sequence_changes'])
        totals['conversations_with_sequence_changes'] += bool(session['ordering']['record_sequence_changes'])
    validation = verify_imports(output, files, chronological=bool(manifest.get('ordering_policy')))
    if validation['runs_imported_to_temporary_db'] != count or validation['records_checked'] != totals['executions']:
        raise ValueError('Sample verification count mismatch')
    write_new(output / 'session-index.json', encoded(selected_sessions))
    selection_raw = encoded(selection)
    write_new(output / 'selection.json', selection_raw)
    result = {**manifest, 'bundle_kind': 'development_sample', 'conversations': count,
              'source_fragments': totals['phases'], 'collection_id': catalog['collection']['id'],
              'counts': dict(totals), 'raw_sources': copied_sources, 'import_files': files,
              'validation': validation,
              'sampling': {'algorithm': selection['algorithm'], 'seed': selection['seed'], 'population_conversations': len(profiles),
                           'source_manifest_sha256': manifest_sha, 'source_session_index_sha256': index_sha,
                           'selection_sha256': digest(selection_raw), 'run_payloads_unchanged': True}}
    from .source_fields import source_audit, require_valid_source
    from .bundle_integrity import BUNDLE_VERSION, file_entry, verify_metadata
    raw_root = output / 'import/raw'
    source_catalog = decode((raw_root / 'builtin-traces/catalog.json').read_bytes())
    documents = {f['member']: decode((raw_root / f['member']).read_bytes())
                 for session in selected_sessions for f in session['fragments']}
    field_audit = source_audit(source_catalog, documents)
    require_valid_source(field_audit)
    write_new(output / 'source-audit.json', encoded(field_audit))
    result['bundle_schema_version'] = BUNDLE_VERSION
    result['auxiliary_files'] = [file_entry(output, name, kind) for name, kind in [
        ('session-index.json', 'session_index'), ('source-audit.json', 'source_audit'), ('selection.json', 'selection')]]
    validation.update(verify_metadata(output, result))
    write_new(output / 'manifest.json', encoded(result))
    report = [f'# 豆包工作 · {count} 个多样化开发样本', '',
              f'从 {len(profiles):,} 个会话中抽取 {count} 个不同会话，保留其全部 {totals["phases"]} 个已有来源片段、{totals["executions"]:,} 条标准记录。', '',
              '按记录规模分层，覆盖不同操作、计时、失败与清洗边界；版本修订时可沿用同一批会话。方法：`' + selection['algorithm'] + '`，种子：`' + str(selection['seed']) + '`。',
              '这是开发与回归测试样本，刻意增加少见情况；不能用样本比例推算全量质量。样本属于全量包，不是与全量隔离的留出测试集。', '',
              '## 规模分层', '', '| 标准记录数 / 会话 | 会话数 |', '| --- | ---: |']
    report.extend(f'| {name} | {n} |' for name, n in selection['size_quotas'].items())
    report += ['', '## 覆盖情况', '', '| 维度 | 样本会话数 | 全量会话数 |', '| --- | ---: | ---: |']
    report.extend(f'| {f} | {selection["sample_features"].get(f, 0)} | {n} |'
                  for f, n in selection['population_features'].items() if not f.startswith(('operation:', 'size:')))
    observed_ops = sum(f.startswith('operation:') for f in selection['sample_features'])
    all_ops = sum(f.startswith('operation:') for f in selection['population_features'])
    report += ['', f'覆盖来源操作标签 {observed_ops}/{all_ops} 种。业务操作分类不改变标准轨迹的 Bash 工具类型。',
               f'覆盖目标未达到项见 selection.json.unmet_targets（{len(selection["unmet_targets"])} 项，公开保留，不补造样本）。', '',
               '## 导入与复现', '',
               '按 manifest.json.import_files 顺序导入 import/catalog.json 与 import/*.trace.json。原始证据、索引、抽样清单不作为标准轨迹上传。',
               '轨迹 JSON 的 ID、字节和哈希与全量相同；先导入样本再导入全量时复用已有运行。新样本有独立集合 ID。',
               'selection.json 保存所有入选 ID、规模特征、入选原因、种子与来源清单哈希；session-index.json 保存全部来源片段及原始 ID。',
               '为保持原有 JSON 来源指针有效，import/raw/builtin-traces/catalog.json 保留全量原始目录（含未抽中会话的摘要元数据）；原始轨迹正文只复制入选会话。',
               '“保留全部片段”仅指本批导出中已有内容。原始会话是否完整、模型耗时和 token 仍未知；标准记录数不等于已确认独立调用数。',
               '样本已通过来源哈希、来源指针、时间顺序和临时数据库实际导入校验；没有写入正在运行的平台或触发分析。', '',
               '## 样本索引', '', '| 会话 ID 前 12 位 | 记录数 | 来源片段 | 计时覆盖 | 失败记录 |', '| --- | ---: | ---: | --- | ---: |']
    report.extend(f'| {p["conversation_id"][:12]} | {p["records"]} | {p["fragments"]} | {p["timing"]} | {p["failed_records"]} |' for p in chosen)
    write_new(output / 'README.md', ('\n'.join(report) + '\n').encode())
    (output / 'INCOMPLETE').unlink()
    return {'state': 'ready', 'output': str(output), 'conversations': count, 'counts': dict(totals),
            'size_quotas': selection['size_quotas'], 'unmet_targets': selection['unmet_targets'],
            'operation_labels_covered': observed_ops, 'operation_labels_in_population': all_ops}
