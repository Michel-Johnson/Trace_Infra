"""Base-specific, evidence-based stage labels. Never executes captured commands."""
import json
import re
import shlex
import sys

STAGES = [('plan', 'Plan · 规划'), ('spec', 'Spec · 设计'), ('table', 'Table · 数据表'),
          ('workflow', 'Flow · 工作流'), ('dashboard', 'Dashboard · 仪表盘'),
          ('form', 'Form · 表单'), ('permission', 'Permission · 权限'),
          ('app', 'App · 应用页面'), ('prepare', '准备 · 文档与环境'),
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

def cli_stage(command):
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|()\n')
        lexer.whitespace = ' \t\r'
        lexer.whitespace_split = True
        tokens = [token.strip() or ';' if '\n' in token and set(token).issubset(set(';&|()\n')) else token for token in lexer]
    except ValueError: return None, ''
    stages = []
    for i, token in enumerate(tokens):
        if token.rsplit('/', 1)[-1] != 'lark-cli': continue
        # Ignore quoted text, grep patterns and echoed command examples.
        if i and tokens[i - 1] not in ('&&', '||', ';', '|', '(', 'command', 'env'): continue
        words = []
        for word in tokens[i + 1:]:
            if word in ('&&', '||', ';', '|', ')'): break
            words.append(word)
        if not words: continue
        if words[0] == 'auth': stages.append('prepare'); continue
        if words[0] != 'base': continue
        action = next((w[1:] for w in words[1:] if w.startswith('+')), '')
        module = action.split('-')[0]
        stage = {'table': 'table', 'field': 'table', 'record': 'table', 'view': 'table',
                 'workflow': 'workflow', 'dashboard': 'dashboard', 'form': 'form',
                 'role': 'permission', 'advperm': 'permission', 'app': 'app',
                 'workspace': 'app', 'template': 'prepare', 'url': 'prepare', 'title': 'prepare'}.get(module)
        if action == 'base-create': stage = 'table'
        elif action == 'base-block-create':
            kind = words[words.index('--type') + 1] if '--type' in words and words.index('--type') + 1 < len(words) else ''
            stage = {'table': 'table', 'workflow': 'workflow', 'dashboard': 'dashboard'}.get(kind)
        elif module == 'base': stage = 'prepare'
        if stage: stages.append(stage)
    return unique(stages, '依据实际 lark-cli Base 命令模块。')

def classify(span, by_id, phase_names):
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
    return {'kind': 'facets', 'data': {'schema_version': 'trace-hunter/facets/1.0', 'input_refs': [context['input_ref']],
            'coverage': 'complete', 'facets': [{'id': 'base.stage', 'title': 'Base 生成阶段', 'cardinality': 'single',
            'values': [{'id': key, 'title': title} for key, title in STAGES]}], 'assignments': assignments},
            'usage': {'input_tokens': 0, 'output_tokens': 0, 'cost': 0, 'currency': None}}

if __name__ == '__main__': print(json.dumps(evaluate(json.load(sys.stdin)), ensure_ascii=False, allow_nan=False))
