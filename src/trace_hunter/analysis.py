"""Platform-owned deterministic analysis. Never trusts imported totals or scores."""
from collections import defaultdict
VERSION='deterministic/1.0.2'
from .timeline import duration, tool_rows
def union(intervals):
    merged=[]
    for a,b in sorted(intervals):
        if merged and a<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],b)
        else:merged.append([a,b])
    return sum(b-a for a,b in merged)
def analyze(trace,phase_ids=None):
    selected=phase_ids if phase_ids is not None else [p['id'] for p in trace['phases'] if p['purpose']=='task']
    allowed={p['id'] for p in trace['phases']}
    if not selected or any(x not in allowed for x in selected):raise ValueError('请选择有效阶段')
    phase_ids=set(selected); phases=[p for p in trace['phases'] if p['id'] in phase_ids]
    def scope_coverage(kind):
        values=[p.get('coverage',trace['coverage'])[kind] for p in phases]
        return 'complete' if all(x=='complete' for x in values) else 'missing' if all(x=='missing' for x in values) else 'partial'
    known_bounds=all(p['start_ms'] is not None and p['end_ms'] is not None for p in phases)
    lo=min(p['start_ms'] for p in phases) if known_bounds else None
    hi=max(p['end_ms'] for p in phases) if known_bounds else None
    wall=hi-lo if known_bounds else None
    spans=[s for s in trace['spans'] if s['phase_id'] in phase_ids]
    models=[s for s in spans if s['kind']=='model']; tools=[s for s in spans if s['kind']=='tool']
    groups=defaultdict(list)
    for s in models:groups[(s['agent_id'],s['request_id'],s['attempt'])].append(s)
    usage_rows=[]; conflicts=[]; duplicates=0
    for key,records in groups.items():
        duplicates+=len(records)-1
        populated=[r['usage'] for r in records if r['usage'] is not None]
        # Identical final snapshots are safe to deduplicate. Conflicts remain unknown.
        unique={tuple(sorted(u.items())) for u in populated}
        if len(unique)>1:conflicts.extend(r['id'] for r in records);continue
        if populated:usage_rows.append(populated[0])
    fields=['input_tokens','output_tokens','cache_read_tokens','cache_write_tokens','thinking_tokens']
    sums={k:sum(u[k] for u in usage_rows if u[k] is not None) if any(u[k] is not None for u in usage_rows) else None for k in fields}
    coverage={k:{'recorded':sum(u[k] is not None for u in usage_rows),'requests':len(groups)} for k in fields}
    if not groups and scope_coverage('model_requests')=='complete':sums={k:0 for k in fields}
    complete=(bool(groups) or scope_coverage('model_requests')=='complete') and scope_coverage('model_requests')=='complete' and all(coverage[k]['recorded']==len(groups) for k in ('input_tokens','output_tokens'))
    total=sums['input_tokens']+sums['output_tokens'] if sums['input_tokens'] is not None and sums['output_tokens'] is not None else None
    rows=tool_rows(trace,phases)
    intervals=[]
    for s in tools:
        a,b=s['start_ms'],s['end_ms']
        if a is not None and b is not None:intervals.append((max(a,lo),min(b,hi)) if known_bounds else (a,b))
    valid_intervals=[(a,b) for a,b in intervals if b>=a]
    tool_union=union(valid_intervals) if valid_intervals else 0 if not tools and scope_coverage('tools')=='complete' else None
    unknown_time=sum(duration(s) is None for s in tools)
    by_operation={op:{'calls':sum(s['operation']==op for s in tools),'tool_ms_sum':sum(duration(s) or 0 for s in tools if s['operation']==op) if any(duration(s) is not None for s in tools if s['operation']==op) else None} for op in sorted({s['operation'] for s in tools})}
    evidence=[e for e in trace['evidence'] if not e['span_ids'] or any(s['id'] in e['span_ids'] for s in spans)]
    checks=[e for e in evidence if e['kind']=='assertion']
    failed=[e['id'] for e in checks if e['status']=='failed']
    errors=[s['id'] for s in tools if s['status']=='error']
    issues=[]
    if errors:issues.append({'code':'tool_error','message':f'{len(errors)} 次工具调用记录为错误','evidence_ids':errors})
    if unknown_time:issues.append({'code':'timing_missing','message':f'{unknown_time} 次工具执行时间缺失','evidence_ids':[s['id'] for s in tools if duration(s) is None]})
    if duplicates:issues.append({'code':'usage_deduplicated','message':f'已合并 {duplicates} 个重复请求分片的 usage','evidence_ids':[]})
    if conflicts:issues.append({'code':'usage_conflict','message':'同一请求存在冲突 usage，已排除累计值','evidence_ids':conflicts})
    if not checks:issues.append({'code':'acceptance_missing','message':'尚无任务验收证据，不能判定业务完成质量','evidence_ids':[]})
    if failed:issues.append({'code':'acceptance_failed','message':f'{len(failed)} 项验收记录未通过','evidence_ids':failed})
    timing_ratio=tool_union/wall if wall and tool_union is not None else None
    return {'analyzer_version':VERSION,'scope':sorted(phase_ids),'wall_ms':wall,'time_basis':trace['run']['time_basis'],'tool_count':len(tools),'tool_error_count':len(errors),'model_request_count':len(groups) if models or scope_coverage('model_requests')=='complete' else None,'request_coverage':scope_coverage('model_requests'),'tool_coverage':scope_coverage('tools'),'tool_time_known':len(tools)-unknown_time,'tool_time_union_ms':tool_union,'tool_time_sum_ms':sum(duration(s) or 0 for s in tools) if any(duration(s) is not None for s in tools) else None,'other_time_ms':max(0,wall-tool_union) if wall is not None and tool_union is not None else None,'tool_time_ratio':timing_ratio,'tokens':{**sums,'total_tokens':total,'complete':complete,'coverage':coverage,'deduplicated_fragments':duplicates,'conflicts':conflicts},'rows':rows,'by_operation':by_operation,'issues':issues,'quality':{'state':'failed' if failed else 'recorded_checks_passed' if checks and all(e['status']=='passed' for e in checks) else 'unknown','checks':checks},'evidence':evidence}
