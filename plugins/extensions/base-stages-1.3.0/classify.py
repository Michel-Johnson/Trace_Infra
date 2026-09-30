"""Base-specific, evidence-based stage labels. Never executes captured commands."""
import json
import re
import sys

# Values are a presentation workflow; calls inside each value retain source order.
STAGES = [('plan', 'Plan · 规划'), ('spec', 'Spec · 设计'),
          ('prepare', 'Skill / CLI · 读取与准备'),
          ('table', 'Table · 数据表'), ('form', 'Form · 表单'),
          ('dashboard', 'Dashboard · 仪表盘'), ('workflow', 'Workflow · 工作流'),
          ('permission', 'Permission · 权限'), ('app', 'App · 应用页面'),
          ('document', 'Document · 内嵌文档'), ('mixed', '跨模块 · 组合调用'),
          ('verify', '验证'), ('delivery', '交付')]
ALIASES = {'flow': 'workflow', 'app_page': 'app', 'app_perm': 'permission',
           'planning': 'plan', 'specification': 'spec', 'verification': 'verify', 'perm': 'permission',
           'spec_lite': 'spec', 'spec_standard': 'spec', 'spec_pro': 'spec',
           'requirements_generator': 'spec', 'requirements_editor': 'spec',
           'data_table_designer': 'spec', 'data_table_editor': 'spec',
           'workflow_designer': 'spec', 'workflow_editor': 'spec',
           'dashboard_designer': 'spec', 'dashboard_editor': 'spec',
           'permission_designer': 'spec', 'permission_editor': 'spec',
           'table_planner': 'table', 'dashboard_planner': 'dashboard', 'perm_planner': 'permission',
           'workflow_adapter_planner_agent': 'workflow', 'agentic_perm_agent': 'permission',
           'agentic_table_record_agent': 'table', 'agentic_table_think_agent': 'table',
           'agentic_table_test_agent': 'verify', 'host_critic_agent': 'verify',
           'host_critic_summarize_agent': 'delivery', 'clarify': 'plan'}
DIRECT = {'create_plan': 'plan', 'update_plan': 'plan', 'generate_plan': 'plan',
          'read_plan': 'plan', 'read_skill': 'prepare',
          'create_table': 'table', 'create_fields': 'table', 'modify_fields': 'table',
          'create_view': 'table', 'modify_view': 'table', 'generate_formula': 'table', 'add_mock_records': 'table',
          'create_role': 'permission', 'modify_role': 'permission',
          'agentic_create_role': 'permission', 'agentic_modify_role': 'permission',
          'create_plan_dashboard': 'dashboard',
          'generate_document': 'spec', 'generate_spec': 'spec', 'nl2spec': 'spec',
          'nl2plan': 'plan', 'all_job_completed': 'delivery', 'present_files': 'delivery'}

def normalized(value):
    return re.sub(r'[^a-z0-9]+', '_', re.sub(r'([a-z])([A-Z])', r'\1_\2', str(value)).lower()).strip('_')

def marker(value):
    name = normalized(value)
    if name in dict(STAGES): return name
    if name in ALIASES: return ALIASES[name]
    if name in DIRECT: return DIRECT[name]
    for stage, _ in STAGES:
        # Actual Base agent keys include SPEC_AGENT_*, TABLE_AGENT_* and WORKFLOW_ADAPTER_AGENT_*.
        if re.search(r'(?:^|_)' + stage + r'_(?:adapter_)?agent(?:_|$)', name): return stage
        if name.startswith(('base_' + stage + '_', stage + '_')) and name.endswith(('_execute', '_generate', '_create_block')): return stage
    if re.search(r'(?:^|_)flow_(?:adapter_)?agent(?:_|$)', name): return 'workflow'
    return None

def unique(candidates, reason):
    found = {x for x in candidates if x}
    if len(found) == 1: return next(iter(found)), reason
    if len(found) > 1: return None, '同一调用包含多个阶段标识，保留未知。'
    return None, ''

def commands(span):
    """Only executable input fields; source labels and result text are not commands."""
    if span['kind'] != 'tool': return []
    tool = normalized(span['name'])
    if span.get('operation') != 'bash' and tool not in ('bash', 'shell', 'exec_command', 'execute_command'): return []
    value = span.get('input')
    if isinstance(value, str):
        try: decoded = json.loads(value)
        except (ValueError, TypeError): decoded = None
        if isinstance(decoded, dict): value = decoded
        else: return [value]
    if not isinstance(value, dict): return []
    if value.get('shared_execution') is True and isinstance(value.get('commands'), list):
        return [s for s in value['commands'] if isinstance(s, str)]
    return [value[k] for k in ('command', 'cmd') if isinstance(value.get(k), str)][:1]

def shell_tokens(command):
    """Small, non-executing lexer retaining the distinction between text and syntax.

    Literal wrapper bodies can be read again; echoed strings, script file contents,
    here-documents and dynamically generated commands are never evaluated.
    """
    tokens, word, quote, quoted = [], '', None, False
    heredocs, awaiting_delimiter = [], False
    i = 0
    def emit():
        nonlocal word, quoted, awaiting_delimiter
        if word or quoted:
            tokens.append((word, False))
            if awaiting_delimiter:
                heredocs.append(word); awaiting_delimiter = False
        word, quoted = '', False
    while i < len(command):
        char = command[i]
        if quote:
            if char == quote:
                quote = None
            elif char == '\\' and quote == '"' and i + 1 < len(command) and command[i+1] in ('"', '\\', '\n'):
                i += 1
                if command[i] != '\n': word += command[i]
            else: word += char
            i += 1; continue
        if char in ('\'', '"'):
            quote, quoted = char, True; i += 1; continue
        if char in ('\\', '`') and i + 1 < len(command) and command[i+1] == '\n':
            i += 2; continue
        if char == '\\' and i + 1 < len(command) and command[i+1] in ' ;&|()\"\'\\':
            i += 1; word += command[i]; i += 1; continue
        if char == '#' and not word:
            end = command.find('\n', i); i = len(command) if end < 0 else end; continue
        if char in ' \t\r': emit(); i += 1; continue
        if char in ';&|()\n':
            emit(); tokens.append((char, True)); i += 1
            if char == '\n' and heredocs:
                for delimiter in heredocs:
                    while i < len(command):
                        end = command.find('\n', i)
                        if end < 0: end = len(command)
                        line = command[i:end].lstrip('\t'); i = min(end+1, len(command))
                        if line == delimiter: break
                    else: return []
                heredocs = []
            continue
        if char in '<>':
            emit(); end = i + 1
            while end < len(command) and command[end] == char: end += 1
            operator = command[i:end]
            if end < len(command) and command[end] == '-': end += 1
            # Redirection target is data. A redirection is not a command boundary.
            if end < len(command) and command[end] == '&':
                end += 1
                while end < len(command) and command[end].isdigit(): end += 1
            tokens.append((operator, False))
            awaiting_delimiter = operator == '<<'
            i = end; continue
        word += char; i += 1
    if quote or heredocs or awaiting_delimiter: return []
    emit()
    return tokens


def executable_segments(command):
    # Function bodies are definitions, not proof of invocation. Resolving a shell
    # call graph is outside this classifier; keep such composite scripts unknown.
    if re.search(r'(?:^|[;\n])\s*(?:function\s+[A-Za-z_]\w*(?:\s*\(\s*\))?|[A-Za-z_]\w*\s*\(\s*\))\s*\{', command):
        return
    segment = []
    for value, control in shell_tokens(command):
        if control:
            if segment: yield segment
            segment = []
        else: segment.append(value)
    if segment: yield segment


def command_head(words):
    """Recognize bounded shell syntax at the executable position, not a text match."""
    words = list(words)
    while words and words[0] in ('then', 'do', '{'): words.pop(0)
    while words and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', words[0], re.S): words.pop(0)
    if len(words) >= 3 and re.fullmatch(r'\$[A-Za-z_][A-Za-z0-9_]*', words[0]) and words[1] == '=':
        words = words[2:]  # PowerShell assignment of a command's output.
    if not words: return []
    name = words[0].replace('\\', '/').rsplit('/', 1)[-1].lower()
    if name in ('command', 'env'):
        words = words[1:]
        while words and (words[0] in ('--', '-i') or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', words[0], re.S)):
            words.pop(0)
    elif name == 'xargs':
        words = words[1:]
        while words and words[0].startswith('-'):
            flag = words.pop(0)
            if flag == '--': break
            if flag in ('-I', '-n', '-L', '-P', '-d', '-E'):
                if not words: return []
                words.pop(0)
            elif flag not in ('-0', '-r', '-t') and not re.fullmatch(r'-(?:I|n|L|P|d|E).+', flag): return []
    return words


def cli_calls(command, depth=0):
    if depth > 3: return []
    calls = []
    for segment in executable_segments(command):
        words = command_head(segment)
        if not words: continue
        executable = words[0].replace('\\', '/').rsplit('/', 1)[-1].lower()
        if executable in ('powershell', 'powershell.exe', 'pwsh', 'pwsh.exe', 'cmd', 'cmd.exe', 'bash', 'sh'):
            flags = ('-command', '-c') if executable.startswith(('powershell', 'pwsh')) else ('/c',) if executable.startswith('cmd') else ('-c', '-lc')
            index = next((i for i, w in enumerate(words[1:], 1) if w.lower() in flags), None)
            if index is not None and index + 2 == len(words):
                calls.extend(cli_calls(words[index+1], depth+1))
            continue
        if executable not in ('lark-cli', 'lark-cli.exe'): continue
        words = words[1:]
        # A bare executable is a help attempt even if redirected into grep/head.
        plain = []
        for word in words:
            if word in ('>', '>>', '<', '<<'): break
            plain.append(word)
        if plain and plain[-1].isdigit() and len(plain) < len(words): plain.pop()
        words = plain
        help_call = '--help' in words or '-h' in words or not words or words[0] == 'help'
        if words and words[0] == 'base':
            action = words[1][1:] if len(words)>1 and re.fullmatch(r'\+[a-z][a-z0-9-]*', words[1]) else ''
            if action:
                calls.append({'id':'lark-cli base +' + action + (' --help' if help_call else ''),
                              'action':action, 'help':help_call, 'stage':action_stage(action, words)})
            elif help_call or len(words) == 1:
                calls.append({'id':'lark-cli base --help','action':'help','help':True, 'stage':'prepare'})
            elif len(words)>1 and re.fullmatch(r'[a-z][a-z-]{0,50}', words[1]):
                # Invalid/legacy spelling remains visible; do not silently add '+'.
                calls.append({'id':'lark-cli base '+words[1],'action':'unrecognized','help':False,'stage':None})
        elif help_call: calls.append({'id':'lark-cli --help','action':'help','help':True,'stage':'prepare'})
        elif words and words[0] == 'auth': calls.append({'id':'lark-cli auth','action':'auth','help':False,'stage':'prepare'})
    return calls

def intent(action):
    if action['help'] or action['action'] in ('help','auth'): return 'read'
    name=action['action']
    if name == 'base-create': return 'create'
    if any(part in name.split('-') for part in ('create','update','upsert','delete','set','add','remove','enable','disable','publish','insert','append','copy','move','rename','import','sync','grant','revoke','bind','submit','upload','arrange')): return 'update'
    if any(part in name.split('-') for part in ('get','list','search','query','resolve','export','download','check','validate','detail','inspect','info')): return 'read'
    return None

def action_stage(action, words):
    module = action.split('-')[0]
    stage = {'table':'table', 'field':'table', 'record':'table', 'view':'table', 'data':'table',
             'button':'table', 'attachment':'table', 'workflow':'workflow', 'dashboard':'dashboard',
             'form':'form', 'role':'permission', 'advperm':'permission', 'app':'app',
             'workspace':'app', 'template':'prepare', 'url':'prepare', 'title':'prepare'}.get(module)
    if action == 'base-create': return 'table'
    if action == 'base-block-create':
        kind = words[words.index('--type')+1] if '--type' in words and words.index('--type')+1 < len(words) else ''
        return {'table':'table','workflow':'workflow','dashboard':'dashboard','docx':'document','document':'document'}.get(kind)
    return 'prepare' if module == 'base' else stage


def cli_stage(command):
    calls = cli_calls(command)
    if not calls: return None, ''
    stages = {'prepare' if call['help'] else call['stage'] for call in calls}
    if None in stages: return None, '命令已识别，但包含未覆盖的 Base 动作或动态类型。'
    if len(stages) > 1:
        return 'mixed', '同次调用明确涉及：' + '、'.join(dict(STAGES)[s] for s in sorted(stages)) + '；记录与耗时仍只计一次。'
    return next(iter(stages)), '依据实际 lark-cli Base 命令模块。'

def classify(span, by_id, phase_names):
    # Loading a skill/reference stays a preparation step even inside a domain Agent.
    payload = span.get('input')
    if isinstance(payload, str):
        try: payload = json.loads(payload)
        except (ValueError, TypeError): payload = {}
    payload = payload if isinstance(payload, dict) else {}
    tool_name = normalized(span['name'])
    path = next((payload.get(k) for k in ('file_path', 'path', 'tail') if isinstance(payload.get(k), str)), '')
    if span['kind'] == 'tool' and (tool_name in ('skill', 'read_skill') or
            tool_name == 'read' and ('lark-base' in path or 'lark-cli' in path or '/references/' in path or path.replace('\\','/').rsplit('/',1)[-1].lower() == 'skill.md')):
        return 'prepare', '明确读取技能或 CLI 参考资料，独立于后续领域执行。'
    shell_commands = commands(span)
    if shell_commands:
        stages = [cli_stage(command) for command in shell_commands]
        # Help still describes preparation when the source names a CLI action.
        calls = [call for command in shell_commands for call in cli_calls(command)]
        if calls and all(call['help'] for call in calls): return 'prepare', '实际执行 CLI 帮助查询，不是业务写入。'
        if any(reason for stage, reason in stages) and not all(stage for stage, _ in stages):
            return None, '命令已识别，但包含未覆盖动作或动态类型；不让已知子命令掩盖缺口。'
        if stages and all(stage for stage, _ in stages):
            distinct = {stage for stage, _ in stages}
            if len(distinct) > 1:
                return 'mixed', '共享执行明确涉及：' + '、'.join(dict(STAGES)[s] for s in sorted(distinct)) + '；记录与耗时仍只计一次。'
            return stages[0]
        found, reason = unique([stage for stage, _ in stages], '依据实际 CLI 输入；工具名称可为 Bash 或来源命令标签。')
        if found or reason: return found, reason
    if span['kind'] == 'tool' and tool_name in ('bash', 'shell', 'exec_command', 'execute_command'):
        command = payload.get('command') or payload.get('cmd') or (span.get('input') if isinstance(span.get('input'), str) else '')
        if isinstance(command, str) and cli_stage(command)[0] == 'prepare':
            return 'prepare', '实际 CLI 帮助、认证或准备命令。'
    stage = marker(span.get('agent_id', ''))
    if not stage and normalized(span.get('name','')) not in DIRECT: stage = marker(span.get('name', ''))
    if stage: return stage, '依据所属 Base Agent 或工具标识。'
    parent = span.get('parent_id'); seen = set()
    while parent in by_id and parent not in seen:
        seen.add(parent); ancestor = by_id[parent]
        stage = marker(ancestor.get('agent_id', '')) or marker(ancestor.get('name', ''))
        if stage: return stage, '依据最近的 Base 父级 Agent 标识。'
        parent = ancestor.get('parent_id')
    stage = marker(phase_names.get(span.get('phase_id'), ''))
    if stage: return stage, '依据轨迹显式记录的阶段名称。'
    stage = DIRECT.get(normalized(span['name']))
    if stage: return stage, '依据 Base 工具标识；有领域 Agent 上下文时优先按领域归属。'
    if span['kind'] != 'tool': return None, '没有明确的 Base 阶段标识。'
    data = span.get('input')
    if isinstance(data, str):
        try: decoded = json.loads(data)
        except (ValueError, TypeError): decoded = None
        if isinstance(decoded, dict): data = decoded
    data = data if isinstance(data, dict) else {'command': data} if isinstance(data, str) else {}
    tool = normalized(span['name'])
    if tool in ('bash', 'shell', 'exec_command', 'execute_command'):
        command = data.get('command') or data.get('cmd') or ''
        if isinstance(command, str):
            stage, reason = cli_stage(command)
            if stage or reason: return stage, reason
    path = next((data.get(k) for k in ('file_path', 'path', 'tail') if isinstance(data.get(k), str)), '')
    filename = path.replace('\\', '/').rsplit('/', 1)[-1].lower()
    if tool in ('read', 'write', 'edit', 'multi_edit'):
        if re.fullmatch(r'(?:base[-_.])?(?:plan|spec)(?:[-_.][a-z0-9_-]+)?\.(?:md|json|ya?ml)', filename):
            kind = 'spec' if re.search(r'(?:^|[-_.])spec(?:[-_.]|$)', filename) else 'plan'
            return kind, '依据明确的 Plan / Spec 文件路径；不扫描正文猜测阶段。'
        if tool == 'read' and ('lark-base' in path or '/references/' in path or filename == 'skill.md'):
            return 'prepare', '读取技能或参考文档；未将文档内容当作已执行的操作。'
    return None, '缺少明确的 Base 阶段证据，保留未知。'

def evaluate(context):
    spans = context['input']['spans']; by_id = {s['id']: s for s in spans}
    phases = {p['id']: p['name'] for p in context['input']['phases']}
    labels = {s['id']: classify(s, by_id, phases) for s in spans}; inferred = {}
    # Attribute an otherwise unknown model request only when all of its invoked tools agree.
    invoked = {}
    for link in context['input']['links']:
        if link['type'] == 'invokes': invoked.setdefault(link['from'], []).append(link['to'])
    for sid, children in invoked.items():
        if sid not in labels or labels[sid][0] or by_id[sid]['kind'] != 'model': continue
        values = {labels[c][0] for c in children if c in labels}
        if len(values) == 1 and None not in values:
            labels[sid] = (next(iter(values)), '依据本次模型请求关联的工具阶段；全部关联工具一致。')
            inferred[sid] = children
    assignments = []
    for s in spans:
        target = {'document_id': context['input']['run']['id'], 'document_digest': context['input_digest'], 'entity_kind': 'span', 'entity_id': s['id']}
        stage, reason = labels[s['id']]
        assignments.append({'target': target, 'facet_id': 'base.stage', 'status': 'assigned' if stage else 'unknown',
                            'value_ids': [stage] if stage else [], 'reason': reason,
                            'evidence_refs': [target, *[{**target, 'entity_id': child} for child in inferred.get(s['id'], [])[:99]]]})
    cli_by_span={s['id']:[call for command in commands(s) for call in cli_calls(command)] for s in spans}
    # Facet schema permits 100 values per dimension; excess labels remain explicitly unknown.
    cli_names=sorted({call['id'] for calls in cli_by_span.values() for call in calls})[:100]
    for s in spans:
        target = {'document_id':context['input']['run']['id'],'document_digest':context['input_digest'],'entity_kind':'span','entity_id':s['id']}
        calls=cli_by_span[s['id']]
        action_values=sorted({value for call in calls if (value:=intent(call))})
        if not calls or any(intent(call) is None for call in calls): action_values=[]
        cli_values=sorted({call['id'] for call in calls})
        if not set(cli_values).issubset(cli_names): cli_values=[]
        for facet,values,reason in [('base.intent',action_values,'按可观测命令区分创建 Base、更新内容与读取；同一 Case 可同时包含创建和更新。'),
                                    ('base.cli',cli_values,'按实际命令模块与动作归一；帮助查询独立，参数不进入标签。多命令共享执行不分摊错误。')]:
            assignments.append({'target':target,'facet_id':facet,'status':'assigned' if values else 'unknown','value_ids':values,
                                'reason':reason if values else '缺少可确认的实际 CLI 命令或超出标签上限。','evidence_refs':[target]})
    facets=[{'id':'base.stage','title':'Base 生成阶段','cardinality':'single','values':[{'id':key,'title':title} for key,title in STAGES]},
            {'id':'base.intent','title':'Base 操作','cardinality':'multiple','values':[{'id':'create','title':'创建 Base'},{'id':'update','title':'更新 Base 内容'},{'id':'read','title':'读取 / 检查'}]},
            {'id':'base.cli','title':'CLI 命令','cardinality':'multiple','values':[{'id':name,'title':name} for name in cli_names] or [{'id':'not_recorded','title':'未记录 CLI'}]}]
    return {'kind': 'facets', 'data': {'schema_version': 'trace-hunter/facets/1.0', 'input_refs': [context['input_ref']],
            'coverage': 'complete', 'facets':facets, 'assignments': assignments},
            'usage': {'input_tokens': 0, 'output_tokens': 0, 'cost': 0, 'currency': None}}

if __name__ == '__main__': print(json.dumps(evaluate(json.load(sys.stdin)), ensure_ascii=False, allow_nan=False))
