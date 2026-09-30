#!/usr/bin/env python3
"""Prepare and verify a self-contained import bundle; never touch the live DB."""
import argparse
import collections
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trace_hunter.corpus_import import VERSION, ORDER_POLICY, STATUS_POLICY, decode, digest, make_catalog, normalize_session
from trace_hunter.import_bundle import MAX_IMPORT_BYTES, verify_imports
from trace_hunter.source_fields import source_audit, require_valid_source
from trace_hunter.import_context import load_context, apply_context
from trace_hunter.bundle_integrity import BUNDLE_VERSION, file_entry, verify_metadata
from trace_hunter.protocol import validate

CATALOG = 'builtin-traces/catalog.json'
MAX_MEMBER = 32 * 1024 * 1024
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_IMPORT = MAX_IMPORT_BYTES


def private_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('xb') as stream:
        path.chmod(0o600)
        stream.write(raw)


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()


def archive_sources(archive, target):
    """Stream once; archive names are data, never passed to tar extraction."""
    members, total = {}, 0
    with tarfile.open(archive, 'r|gz') as stream:
        for member in stream:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or '\\' in member.name:
                raise ValueError('Unsafe archive member path')
            if not path.parts or path.parts[0] != 'builtin-traces':
                raise ValueError('Unexpected archive root')
            if member.isdir():
                continue
            if not member.isfile() or path.suffix != '.json':
                raise ValueError('Only regular JSON files are accepted')
            name = str(path)
            total += member.size
            if name in members or member.size > MAX_MEMBER or total > MAX_ARCHIVE or len(members) >= 12000:
                raise ValueError('Duplicate member or archive size limit exceeded')
            raw = stream.extractfile(member).read(MAX_MEMBER + 1)
            if len(raw) != member.size:
                raise ValueError('Archive member size mismatch')
            decode(raw)
            private_write(target / name, raw)
            members[name] = {'sha256': digest(raw), 'bytes': len(raw)}
    if CATALOG not in members:
        raise ValueError('Missing builtin-traces catalog')
    catalog = decode((target / CATALOG).read_bytes())
    if not isinstance(catalog, dict) or type(catalog.get('version')) is not int or catalog['version'] != 1 or not isinstance(catalog.get('traces'), list):
        raise ValueError('Unsupported source catalog')
    if type(catalog.get('traceCount')) is not int or catalog['traceCount'] != len(catalog['traces']):
        raise ValueError('Catalog trace count mismatch')
    # Fail with precise source field paths before identity/path operations.
    require_valid_source(source_audit(catalog, {}))
    ids, paths, pairs = set(), set(), set()
    for index, entry in enumerate(catalog['traces']):
        if set(entry) & {'_catalog_index', '_member', '_sha256'}:
            raise ValueError('Source entry uses reserved adapter metadata keys')
        if not isinstance(entry.get('id'), str) or not entry['id']:
            raise ValueError('Trace ID required')
        if not isinstance(entry.get('conversationId'), str) or not entry['conversationId']:
            raise ValueError('Conversation ID required')
        if not re.fullmatch(r'\d+', str(entry.get('messageIndex', ''))):
            raise ValueError('Numeric messageIndex required')
        # Catalog paths are URL asset paths with one leading slash, not local paths.
        member = entry['path'].removeprefix('/')
        pair = (entry['conversationId'], int(entry['messageIndex']))
        if member not in members or member == CATALOG or entry['id'] in ids or member in paths or pair in pairs:
            raise ValueError('Catalog identity collision or missing trace member')
        ids.add(entry['id']); paths.add(member); pairs.add(pair)
        entry.update(_catalog_index=index, _member=member, _sha256=members[member]['sha256'])
    if paths != set(members) - {CATALOG}:
        raise ValueError('Unreferenced JSON file in archive')
    return catalog, members


def audit_sources(raw_root, catalog):
    documents = {e['_member']: decode((raw_root / e['_member']).read_bytes()) for e in catalog['traces']}
    return source_audit(catalog, documents)


def inspect_archive(archive):
    with tempfile.TemporaryDirectory(prefix='doubao-source-inspect-') as temp:
        root = Path(temp)
        catalog, _ = archive_sources(Path(archive), root)
        return audit_sources(root, catalog)


def prepare(archive, output, context_path=None):
    archive, output = Path(archive).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Use a new output directory; previous bundles are immutable')
    output.mkdir(parents=True, mode=0o700)
    private_write(output / 'INCOMPLETE', b'Conversion or verification has not finished.\n')
    import_root = output / 'import'
    raw_root = import_root / 'raw'
    with archive.open('rb') as source:
        hasher = hashlib.sha256()
        for block in iter(lambda: source.read(1024 * 1024), b''):
            hasher.update(block)
    archive_sha = hasher.hexdigest()
    catalog, members = archive_sources(archive, raw_root)
    field_audit = audit_sources(raw_root, catalog)
    private_write(output / 'source-audit.json', encode(field_audit))
    require_valid_source(field_audit)
    conversations = collections.defaultdict(list)
    for entry in catalog['traces']:
        conversations[entry['conversationId']].append(entry)
    print(f"Read {len(catalog['traces'])} fragments / {len(conversations)} conversations", flush=True)
    context = load_context(context_path, set(conversations))
    if context:
        private_write(raw_root / 'import-context.json', context['raw'])
        members['import-context.json'] = {'sha256': context['sha256'], 'bytes': len(context['raw'])}
    traces, sessions, files = [], [], []
    declared_cases = {}
    totals = collections.Counter()
    for conversation in sorted(conversations):
        entries = conversations[conversation]
        documents = {e['id']: decode((raw_root / e['_member']).read_bytes()) for e in entries}
        trace, audit = normalize_session(entries, documents, archive_sha, members[CATALOG]['sha256'])
        declared_case = apply_context(trace, audit, context)
        if declared_case:
            declared_cases[declared_case['query_id']] = declared_case
        validate(trace)
        raw = encode(trace)
        if len(raw) > MAX_IMPORT:
            raise ValueError('Normalized session exceeds 16 MiB import limit: ' + trace['run']['id'])
        name = trace['run']['id'] + '.trace.json'
        private_write(import_root / name, raw)
        files.append({'path': 'import/' + name, 'sha256': digest(raw), 'bytes': len(raw),
                      'run_id': audit['run_id'], 'query_id': audit['query_id']})
        for key in ('raw_items', 'duplicate_items_removed', 'shared_executions', 'shared_commands', 'executions'):
            totals[key] += audit[key]
        totals['phases'] += len(trace['phases'])
        totals['phase_windows_extended'] += sum(x['reason'] == 'window_extended_to_observed_calls' for x in audit['changes'])
        totals['timing_conflicts'] += sum(x['reason'] in ('conflicting_shared_timing', 'invalid_duration', 'inconsistent_timing') for x in audit['changes'])
        totals['timed_executions'] += sum(s['duration_ms'] is not None or (s['start_ms'] is not None and s['end_ms'] is not None) for s in trace['spans'])
        repeats = audit['cross_fragment_repeat_candidates']
        totals['cross_fragment_repeat_candidates'] += len(repeats)
        totals['cross_fragment_exact_repeats'] += sum(x['basis'] == 'exact_record' for x in repeats)
        totals['conversations_with_repeat_candidates'] += bool(repeats)
        totals['records_with_sequence_changes'] += len(audit['ordering']['record_sequence_changes'])
        totals['conversations_with_sequence_changes'] += bool(audit['ordering']['record_sequence_changes'])
        totals.update(audit['normalization'])
        sessions.append(audit)
        # Keep only the header needed for the catalog, not all session payloads.
        traces.append({'run': trace['run'], 'origin': audit['ordering']['time_origin_epoch_ms']})
    traces.sort(key=lambda t: (t['origin'] is None, t['origin'] or 0, t['run']['id']))
    positions = {t['run']['id']: i for i, t in enumerate(traces)}
    sessions.sort(key=lambda s: positions[s['run_id']])
    files.sort(key=lambda f: positions[f['run_id']])
    output_catalog = make_catalog(traces, archive_sha, declared_cases, context['sha256'] if context else None)
    catalog_raw = encode(output_catalog)
    if len(catalog_raw) > MAX_IMPORT:
        raise ValueError('Catalog exceeds 16 MiB import limit')
    private_write(import_root / 'catalog.json', catalog_raw)
    files.insert(0, {'path': 'import/catalog.json', 'sha256': digest(catalog_raw), 'bytes': len(catalog_raw), 'kind': 'catalog'})
    print(f"Normalized {len(sessions)} runs / {totals['executions']} execution spans; verifying imports", flush=True)
    verification = verify_imports(output, files, chronological=True,
                                  progress=lambda n: print(f'Verified {n}/{len(sessions)}', flush=True))
    assert verification['runs_imported_to_temporary_db'] == len(sessions)
    private_write(output / 'session-index.json', encode(sessions))
    manifest = {'state': 'ready', 'bundle_schema_version': BUNDLE_VERSION,
                'auxiliary_files': [file_entry(output, name, kind) for name, kind in
                                    [('session-index.json', 'session_index'), ('source-audit.json', 'source_audit')]], 'converter_version': VERSION, 'schema_version': 'trace-hunter/1.1',
                'source_archive': archive.name, 'source_archive_sha256': archive_sha,
                'source_fragments': len(catalog['traces']), 'conversations': len(sessions),
                'collection_id': output_catalog['collection']['id'], 'counts': dict(totals),
                'independent_execution_count': None,
                'count_semantics': 'executions counts normalized command execution records, not confirmed unique underlying executions',
                'ordering_policy': ORDER_POLICY,
                'status_policy': STATUS_POLICY,
                'import_files': files, 'raw_sources': members,
                'validation': verification,
                'identity_policy': 'One partial run per conversationId. Explicit user declarations in import-context override placeholders; no inferred benchmark match.',
                'timing_policy': 'Source timing retained; explicitly shared commands form one execution; no model time or token inferred.'}
    if context:
        manifest['import_context_sha256'] = context['sha256']
    verification.update(verify_metadata(output, manifest))
    private_write(output / 'manifest.json', encode(manifest))
    readme = f'''# 豆包工作 · 可导入数据包

已转换 {len(sessions):,} 份会话轨迹，保留 {len(catalog['traces']):,} 个来源片段（阶段）。
原始 {totals['raw_items']:,} 项命令，移除 {totals['duplicate_items_removed']:,} 项完全重复记录。
{totals['shared_commands']} 条命令归入 {totals['shared_executions']} 次共享执行，最终为 {totals['executions']:,} 个标准化记录方格。
其中有 {totals['cross_fragment_repeat_candidates']:,} 项跨片段重复候选，分布在 {totals['conversations_with_repeat_candidates']} 个会话。
这些候选全部保留并标注，独立执行次数未知；不能将方格数量直接当作真实执行次数。

导入平台时，选择 `import/catalog.json` 和 `import/*.trace.json`。`import/raw/` 是原始证据，不作为标准 JSON 导入。
也可读取 `manifest.json` 的 `import_files`，按所列顺序逐个提交 `/api/import`。

全部文件通过协议、来源哈希与临时数据库实际导入验证；没有写入当前平台，也没有触发分析。

同一个 conversationId 合为一份部分会话；messageIndex 保留为阶段名称，不冒充完整用户轮次。
会话和片段按已观测时间排列；已知工具开始时间按时间升序，缺失时间的记录按原始顺序保留在相邻时间锚点附近。
顺序、messageIndex 和工具时间发生冲突时，保留原始值并在 session-index / evidence 记录新的展示序号；不填补时间。
query_id 与 env_id 默认是会话占位；若提供 import-context 则使用用户明确声明的题目/环境/模型映射并绑定来源哈希。
缺失的模型请求、token 和完整观测对话始终不推测。Case 中声明的题目轮次不等于实际观测轮次。
多个 lark-cli 命令属于同一共享执行时，输入和输出保留所有命令及返回；耗时只归属该执行一次。
同名/相同命令的不同序号调用保留；只移除 ID 和全部内容完全相同的重复项，映射在 evidence 与 session-index.json 中。
片段间短 ID 的作用域没有来源定义，因此跨片段相同记录只建立候选引用，不跨片段删去或借用耗时。
{totals['phase_windows_extended']} 个来源阶段窗口扩展至该片段已记录调用的结束边界，原边界保留在来源及 evidence。
源成功/失败标签只表示记录项，不代表整项任务的业务质量。缺失模型耗时不从日志间隔补出。

调用输入统一为 command 对象（共享执行为 commands 数组）；完整 JSON 返回解码一层，文本原样保留。
进程退出码、原始状态及显式业务 ok 值分别保存在每条 record-facts 证据中。
进程非零退出、业务明确 ok=false 或来源明确 failed 均表示该记录失败；不把退出码 0 等同于业务成功。
本包有 {totals['source_status_corrections']} 条记录依据显式失败证据修正了来源状态，原始标签和判断规则均可追溯。
不扫描任意文字中的 error/失败 关键词判断状态，不把失败记录删除或改成成功。

manifest.json 是批量导入清单与核验结果；session-index.json 保留原会话、来源分组、messageIndex 和清洗映射。
source-audit.json 逐字段报告类型、缺失、空值、未知字段及来源计数偏差。三个文件的作用不同；索引及字段报告均纳入哈希校验。
本包字段检查：{field_audit["errors"]} 个结构错误，{field_audit["warnings"]} 个来源一致性提醒；详情见 source-audit.json。
本包可用仓库 scripts/trace_import.py verify --bundle <目录> 重新校验，pack --bundle <目录> --archive <新文件.tar.gz> 重新打包。
源归档 SHA-256：{archive_sha}
'''
    private_write(output / 'README.md', readme.encode())
    (output / 'INCOMPLETE').unlink()
    print(json.dumps({'output': str(output), 'runs': len(sessions), 'counts': dict(totals), 'state': 'ready'}, ensure_ascii=False), flush=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--context', type=Path)
    args = parser.parse_args()
    prepare(args.archive, args.output, args.context)


if __name__ == '__main__':
    main()
