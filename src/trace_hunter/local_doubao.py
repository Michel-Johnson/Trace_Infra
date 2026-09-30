"""Offline Doubao Work snapshots. No network, authentication, or command execution.

Only allowlisted session files and explicitly selected SDK log days are read.
Original bytes remain separate from parsed messages and optional timing joins.
"""
import collections
import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .adapters import span
from .identity import unknown_environment
from .ordering import decorate
from .protocol import validate

DEFAULT_ROOT = Path.home() / 'Library/Application Support/DoubaoWork'
SESSIONS = Path('Default/.doubaowork/agent_mode/workspace/.sessions')
LOGS = Path('sdk_storage/log')
ID = re.compile(r'^[A-Za-z0-9_-]{1,120}$')
LOG_NAME = re.compile(r'^saman_(\d{4})\.(\d{4})\.(\d+)\.log$')
STAMP = re.compile(r'^\[\d+:\d+:(\d{4})/(\d{6}\.\d+):')
VERSION = '0.1.0'


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def write_private(path, raw):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as stream:
        stream.write(raw)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()


def reject_nonfinite(value):
    raise ValueError('nonfinite JSON constant')


def open_relative(root, relative, *, directory=False):
    """Traverse below the explicit root without following child symlinks."""
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in ('.', '..') for p in parts):
        raise ValueError('relative path required')
    fd = os.open(Path(root).resolve(), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for i, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if directory or i < len(parts) - 1:
                flags |= os.O_DIRECTORY
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def children(root, relative, directories=True):
    fd = open_relative(root, relative, directory=True)
    try:
        with os.scandir(fd) as entries:
            return sorted(e.name for e in entries if
                          (e.is_dir(follow_symlinks=False) if directories
                           else e.is_file(follow_symlinks=False)))
    finally:
        os.close(fd)


def fingerprint(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def read_snapshot(root, relative, max_bytes):
    began = now()
    fd = open_relative(root, relative)
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('regular file required')
        if before.st_size > max_bytes:
            raise ValueError('file_byte_limit')
        raw = source.read(before.st_size)
        after = os.fstat(source.fileno())
    try:
        check = open_relative(root, relative)
        try:
            path_after = os.fstat(check)
        finally:
            os.close(check)
        stable = fingerprint(before) == fingerprint(after) == fingerprint(path_after)
    except OSError:
        stable = False
    return raw, {'source_relative': str(relative), 'bytes': len(raw), 'sha256': digest(raw),
                 'source_mtime_ns': before.st_mtime_ns,
                 'stable_during_read': stable and len(raw) == before.st_size,
                 'read_started_at': began, 'read_ended_at': now()}


def parse_messages(raw):
    records, issues = [], []
    for number, line in enumerate(raw.splitlines(keepends=True), 1):
        if not line.strip():
            continue
        if not line.endswith(b'\n'):
            issues.append({'line': number, 'reason': 'unterminated_tail_preserved_only_in_raw'})
            continue
        try:
            value = json.loads(line, parse_constant=reject_nonfinite)
            if not isinstance(value, dict):
                raise ValueError('non-object')
        except (ValueError, UnicodeDecodeError):
            issues.append({'line': number, 'reason': 'invalid_json_record_preserved_only_in_raw'})
            continue
        records.append({'source_line': number, 'message': value})
    return records, issues


def list_sessions(root=DEFAULT_ROOT):
    result = []
    for sid in children(root, SESSIONS):
        if not ID.fullmatch(sid):
            continue
        try:
            agents = children(root, SESSIONS / sid / 'agents')
        except OSError:
            continue
        rows = []
        for agent in agents:
            try:
                fd = open_relative(root, SESSIONS / sid / 'agents' / agent / 'system/trajectory.jsonl')
                try:
                    st = os.fstat(fd)
                finally:
                    os.close(fd)
                rows.append({'agent_id': agent, 'bytes': st.st_size,
                             'file_modified_at': datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()})
            except OSError:
                pass
        if rows:
            result.append({'conversation_id': sid, 'agents': rows,
                           'note': 'File modification time is not model completion time.'})
    return result


def calls(message):
    value = message.get('tool_calls')
    return [c for c in value if isinstance(c, dict)] if isinstance(value, list) else []


def function(call):
    value = call.get('function')
    return value if isinstance(value, dict) else {}


def arguments(call):
    value = function(call).get('arguments')
    if isinstance(value, str):
        try:
            return json.loads(value, parse_constant=reject_nonfinite)
        except ValueError:
            return value
    return value


def field(line, key):
    found = re.search(r'\b' + re.escape(key) + r'=([^,\s]+)', line)
    return found[1] if found else None


def log_stamp(line, filename, zone):
    stamp = STAMP.match(line)
    name = LOG_NAME.fullmatch(filename)
    if not stamp or not name or not zone:
        return None
    try:
        value = datetime.strptime(name[1] + stamp[1] + stamp[2], '%Y%m%d%H%M%S.%f')
        # Do not infer a missing log year, or silently reinterpret another day.
        if stamp[1] != name[2]:
            return None
        return value.replace(tzinfo=zone).isoformat()
    except ValueError:
        return None


def collect_bash_timing(root, agents, days, timezone_name, max_scan_bytes):
    """Join only unique, complete exact commands. No nearest-time matching."""
    commands = collections.Counter()
    for agent in agents:
        for record in agent['messages']:
            for call in calls(record['message']):
                if function(call).get('name') != 'Bash':
                    continue
                args = arguments(call)
                if isinstance(args, dict) and isinstance(args.get('command'), str):
                    commands[args['command']] += 1
    zone = ZoneInfo(timezone_name) if timezone_name else None
    begin, end = collections.defaultdict(list), collections.defaultdict(list)
    source_files, issues = [], []
    if not days:
        return [], [], ['sdk_log_scan_not_requested']
    try:
        names = children(root, LOGS, directories=False)
    except OSError:
        return [], [], ['sdk_log_directory_unavailable']
    names = [n for n in names if LOG_NAME.fullmatch(n)
             and LOG_NAME.fullmatch(n)[1] + LOG_NAME.fullmatch(n)[2] in days]
    names.sort(key=lambda n: tuple(map(int, LOG_NAME.fullmatch(n).groups())))
    remaining, record_order = max_scan_bytes, 0
    for name in names:
        relative = LOGS / name
        with os.fdopen(open_relative(root, relative), 'rb') as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > remaining:
                issues.append('scan_byte_limit:' + name)
                continue
            remaining -= before.st_size
            offset, line_no, scanned = 0, 0, hashlib.sha256()
            while offset < before.st_size:
                start_offset = offset
                line_raw = stream.readline(min(before.st_size - offset, 2 * 1024 * 1024))
                if not line_raw:
                    break
                offset += len(line_raw)
                scanned.update(line_raw)
                line_no += 1
                record_order += 1
                if not line_raw.endswith(b'\n'):
                    issues.append('oversize_or_unterminated_log_line:' + name)
                    # Discard the remainder of this physical line, not a fake next record.
                    while offset < before.st_size and not line_raw.endswith(b'\n'):
                        line_raw = stream.readline(min(before.st_size - offset, 2 * 1024 * 1024))
                        if not line_raw:
                            break
                        offset += len(line_raw)
                        scanned.update(line_raw)
                    continue
                if b'[sandbox]' not in line_raw or not (
                    b'ContinueRunBash [bash] run begin' in line_raw or
                    b'OnRunBashFinished [bash] run end' in line_raw):
                    continue
                try:
                    line = line_raw.decode('utf-8')
                except UnicodeDecodeError:
                    continue
                iid, rid = field(line, 'instance_id'), field(line, 'bash_run_id')
                if not iid or not rid:
                    continue
                evidence = {'source_relative': str(relative), 'source_line': line_no,
                            'byte_offset': start_offset, 'raw_line': line,
                            'at': log_stamp(line, name, zone), 'record_order': record_order}
                if 'ContinueRunBash [bash] run begin' in line:
                    match = re.search(r'command="((?:\\.|[^"\\])*)", env_len=', line)
                    if not match:
                        continue
                    try:
                        command = json.loads('"' + match[1] + '"')
                        complete = len(command.encode()) == int(field(line, 'command_len'))
                    except (ValueError, TypeError):
                        continue
                    if complete and command in commands:
                        begin[(iid, rid)].append({'command': command, **evidence})
                else:
                    # Metadata only until we know this ID matches an allowed command.
                    end[(iid, rid)].append(evidence)
            after = os.fstat(stream.fileno())
            source_files.append({'source_relative': str(relative), 'scanned_bytes': offset,
                                 'scanned_prefix_sha256': scanned.hexdigest(),
                                 'size_at_open': before.st_size,
                                 'stable_during_scan': fingerprint(before) == fingerprint(after)})
    if not names:
        issues.append('no_matching_sdk_log_files')
    candidates = []
    counts = collections.Counter(s['command'] for starts in begin.values() for s in starts)
    for (iid, rid), starts in begin.items():
        ends = end.get((iid, rid), [])
        if len(starts) != 1 or len(ends) != 1:
            issues.append('ambiguous_or_missing_bash_end:' + rid)
            continue
        a, b = dict(starts[0]), ends[0]
        if b['record_order'] <= a['record_order']:
            issues.append('sdk_end_precedes_start:' + rid)
            continue
        try:
            duration = int(field(b['raw_line'], 'duration_ms'))
            if duration < 0:
                raise ValueError()
        except (TypeError, ValueError):
            issues.append('invalid_sdk_duration:' + rid)
            continue
        if a['at'] and b['at']:
            delta = (datetime.fromisoformat(b['at']) - datetime.fromisoformat(a['at'])).total_seconds() * 1000
            if delta < 0 or abs(delta - duration) > max(10, duration * .01):
                issues.append('clock_duration_conflict:' + rid)
                continue
        candidates.append({'instance_id': iid, 'bash_run_id': rid, 'command': a.pop('command'),
                           'start': a, 'end': b, 'duration_ms': duration,
                           'exit_code': field(b['raw_line'], 'exit_code'),
                           'basis': 'unique_exact_command_within_explicit_log_scope; not shared native ID'})
    for c in candidates:
        c['linkable'] = commands[c['command']] == counts[c['command']] == 1
    return candidates, source_files, sorted(set(issues))


def build_trace(export, export_path, run_id, query_id, env_id, query, model, isolation, network):
    raw = export_path.read_bytes()
    source = lambda pointer: {'source_id': 'local', 'pointer': pointer}
    trace = {'schema_version': 'trace-hunter/1.1',
             'run': {'id': run_id, 'query_id': query_id, 'env_id': env_id,
                     'title': '本地采集 · ' + query[:200], 'query': query,
                     'harness': 'Doubao Work', 'model': model, 'status': 'partial',
                     'time_basis': '相对首条已关联 SDK 记录；非任务提交时间。模型时间未知。'},
             'collector': {'name': 'doubao-local-files', 'version': VERSION},
             'sources': [{'id': 'local', 'name': export_path.name, 'sha256': digest(raw)}],
             'coverage': {'tools': 'partial', 'model_requests': 'missing'},
             'environment': {**unknown_environment(), 'isolation': isolation, 'network_access': network,
                             'observed_at': export['captured_at'],
                             'notes': '本地文件快照；模型标签由采集参数提供；不等于后台完整轨迹。'},
             'phases': [{'id': 'task', 'name': '本地可见任务记录', 'purpose': 'task',
                         'start_ms': None, 'end_ms': None}], 'spans': [], 'links': [], 'evidence': []}
    runtime = {c['command']: (i, c) for i, c in enumerate(export['runtime_calls']) if c['linkable']}
    linked = []
    for ai, agent in enumerate(export['agents']):
        counts = collections.Counter(str(c.get('id')) for r in agent['messages']
                                     for c in calls(r['message']))
        results = collections.defaultdict(list)
        for record in agent['messages']:
            message = record['message']
            if message.get('role') == 'tool' and isinstance(message.get('tool_call_id'), str) and message['tool_call_id']:
                results[message['tool_call_id']].append(message)
        for mi, record in enumerate(agent['messages']):
            message = record['message']
            for ci, call in enumerate(message.get('tool_calls') if isinstance(message.get('tool_calls'), list) else []):
                if not isinstance(call, dict): continue
                sid = f"{agent['id']}:{mi}:{ci}"
                name = function(call).get('name')
                name = name if isinstance(name, str) and name else '未知工具'
                result = results.get(call['id'], []) if isinstance(call.get('id'), str) and call['id'] else []
                result = result[0] if len(result) == 1 and counts[str(call.get('id'))] == 1 else None
                item = span(sid, name, agent_id=agent['id'], input=arguments(call),
                            output=result.get('content') if result else None,
                            status='error' if result and result.get('is_error') is True else 'unknown',
                            source=source(f'/agents/{ai}/messages/{mi}/message/tool_calls/{ci}'))
                args = arguments(call)
                command = args.get('command') if name == 'Bash' and isinstance(args, dict) else None
                if command in runtime:
                    ri, timing = runtime[command]
                    item['duration_ms'] = timing['duration_ms']
                    if timing['exit_code'] is not None:
                        item['status'] = 'ok' if timing['exit_code'] == '0' else 'error'
                    linked.append((item, timing))
                    trace['evidence'].append({'id': 'timing-' + sid, 'kind': 'note',
                                             'name': '本地 SDK 计时关联', 'status': 'observed',
                                             'detail': timing['basis'] + '；exit_code 是进程状态，不代表业务验收通过。',
                                             'span_ids': [sid], 'source': source(f'/runtime_calls/{ri}')})
                # Known order inside one transcript; cross-agent ordering is not observed.
                item['sequence'] = len(trace['spans'])
                item['order_basis'] = 'source_sequence' if len(export['agents']) == 1 else 'unknown'
                trace['spans'].append(item)
    starts = [datetime.fromisoformat(t['start']['at']) for _, t in linked if t['start']['at'] and t['end']['at']]
    if starts:
        origin = min(starts)
        for item, timing in linked:
            if timing['start']['at'] and timing['end']['at']:
                item['start_ms'] = (datetime.fromisoformat(timing['start']['at']) - origin).total_seconds() * 1000
                item['end_ms'] = (datetime.fromisoformat(timing['end']['at']) - origin).total_seconds() * 1000
    trace['evidence'].append({'id': 'coverage', 'kind': 'note', 'name': '本地采集范围',
                             'status': 'observed', 'span_ids': [], 'source': source('/coverage'),
                             'detail': '保留原生工具顺序；模型请求、token、压缩记录完整性未知。'
                                       '多 agent 时仅按文件分组展示，不能视作全局时间顺序。'
                                       'Bash 时间仅按完整且唯一的命令文本关联；其他工具时间留空。'})
    return validate(decorate(trace))


def capture(root, session_id, output, *, query_id, env_id, query=None, model='未知', run_id=None,
            isolation='unknown', network='unknown', log_days=(), log_timezone=None,
            max_file_bytes=32 * 1024 * 1024, max_total_bytes=128 * 1024 * 1024,
            max_log_bytes=512 * 1024 * 1024):
    if not ID.fullmatch(session_id):
        raise ValueError('invalid conversation ID')
    output = Path(output).resolve()
    if output.is_relative_to(Path(root).resolve()):
        raise ValueError('capture output must be outside the client data directory')
    base = SESSIONS / session_id
    agent_ids = children(root, base / 'agents')
    if len(agent_ids) > 50:
        raise ValueError('agent_limit: maximum 50 agents per snapshot')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest = {'version': VERSION, 'state': 'collecting', 'conversation_id': session_id,
                'captured_at': now(), 'files': [], 'omissions': [], 'agents': [],
                'complete_trajectory': False, 'snapshot_atomic_across_files': False}
    write_private(output / 'manifest.in-progress.json', json_bytes(manifest))
    paths = ['board.md', 'memory/MEMORY.md']
    for agent in agent_ids:
        paths.extend(f'agents/{agent}/{tail}' for tail in
                     ('system/trajectory.jsonl', 'system/assignment.md', 'socket/briefing.md', 'socket/findings.jsonl'))
    remaining = max_total_bytes
    agents = []
    for rel in paths:
        relative = base / rel
        try:
            raw, entry = read_snapshot(root, relative, min(max_file_bytes, remaining))
        except FileNotFoundError:
            # Optional notes are not required; missing transcripts are material.
            if rel.endswith('trajectory.jsonl'):
                manifest['omissions'].append({'path': rel, 'reason': 'missing_transcript'})
            continue
        except (OSError, ValueError) as error:
            manifest['omissions'].append({'path': rel, 'reason': type(error).__name__})
            continue
        remaining -= len(raw)
        entry['archive_relative'] = 'raw/' + rel
        write_private(output / entry['archive_relative'], raw)
        manifest['files'].append(entry)
        if rel.endswith('trajectory.jsonl'):
            messages, issues = parse_messages(raw)
            agent = {'id': Path(rel).parts[1], 'raw_ref': entry['archive_relative'],
                     'messages': messages, 'parse_issues': issues,
                     'stable_during_read': entry['stable_during_read']}
            agents.append(agent)
            manifest['agents'].append({'id': agent['id'], 'messages': len(messages),
                                       'parse_issues': issues, 'stable_during_read': entry['stable_during_read']})
    if not agents:
        raise ValueError('No readable native transcript; partial evidence remains in output directory.')
    if query is None:
        query = next((r['message']['content'] for a in agents for r in a['messages']
                      if r['message'].get('role') == 'user' and isinstance(r['message'].get('content'), str)
                      and r['message']['content'].strip()), None)
    if not query or len(query) > 4096:
        raise ValueError('Provide --query with 1–4096 characters; raw messages have been preserved.')
    runtime, logs, log_issues = collect_bash_timing(root, agents, log_days, log_timezone, max_log_bytes)
    export = {'format': 'doubao-local-files/0.1', 'captured_at': manifest['captured_at'],
              'conversation_id': session_id, 'agents': agents, 'runtime_calls': runtime,
              'coverage': {'whole_trajectory': 'unverified', 'model_requests': 'missing',
                           'token_usage': 'missing', 'log_days': list(log_days),
                           'log_timezone': log_timezone, 'log_issues': log_issues},
              'source_files': manifest['files'], 'log_sources': logs}
    local_json = output / 'session.local.json'
    write_private(local_json, json_bytes(export))
    trace = build_trace(export, local_json, run_id or 'doubao-local-' + session_id,
                        query_id, env_id, query, model, isolation, network)
    write_private(output / 'run.trace.json', json_bytes(trace))
    text = ['# ' + query, '', '本地消息快照；文件顺序不代表完整模型请求或后台时间。', '']
    for agent in agents:
        text.extend(['## Agent ' + agent['id'], ''])
        for r in agent['messages']:
            m = r['message']
            text.extend([f"### {m.get('role', 'unknown')} · 原始行 {r['source_line']}", ''])
            body = json.dumps(m, ensure_ascii=False, indent=2)
            fence = '`' * max(3, max((len(s) + 1 for s in re.findall(r'`+', body)), default=3))
            text.extend([fence + 'json', body, fence, ''])
    write_private(output / 'messages.md', '\n'.join(text).encode())
    manifest.update(state='closed', closed_at=now(), log_sources=logs, log_issues=log_issues,
                    normalized_tool_calls=len(trace['spans']),
                    tools_with_sdk_duration=sum(s['duration_ms'] is not None for s in trace['spans']),
                    all_allowlisted_reads_stable=all(f['stable_during_read'] for f in manifest['files']),
                    note='本地采集完成不代表任务完成。输出仅覆盖允许的文件与指定日志日期。')
    manifest['outputs'] = [{'name': f.name, 'sha256': digest(f.read_bytes()), 'bytes': f.stat().st_size}
                           for f in (local_json, output / 'run.trace.json', output / 'messages.md')]
    write_private(output / 'manifest.json', json_bytes(manifest))
    return manifest


def session_fingerprint(root, session_id):
    """Cheap change detection for bounded polling; not a completeness check."""
    if not ID.fullmatch(session_id):
        raise ValueError('invalid conversation ID')
    base = SESSIONS / session_id
    names = ['board.md', 'memory/MEMORY.md']
    for agent in children(root, base / 'agents'):
        names.extend(f'agents/{agent}/{tail}' for tail in
                     ('system/trajectory.jsonl', 'system/assignment.md', 'socket/briefing.md', 'socket/findings.jsonl'))
    result = []
    for name in names:
        try:
            fd = open_relative(root, base / name)
            try:
                result.append((name, fingerprint(os.fstat(fd))))
            finally:
                os.close(fd)
        except OSError:
            result.append((name, None))
    return result
