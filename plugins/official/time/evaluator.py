"""Deterministic time facts. Reads one frozen input and writes one score JSON."""
import json
import sys


def union(intervals):
    merged=[]
    for start,end in sorted(intervals):
        if merged and start<=merged[-1][1]:merged[-1][1]=max(end,merged[-1][1])
        else:merged.append([start,end])
    return sum(end-start for start,end in merged)


def evaluate(context):
    data=context['input'];phases=data['phases'];spans=data['spans']
    bounds=all(p['start_ms'] is not None and p['end_ms'] is not None for p in phases)
    low=min(p['start_ms'] for p in phases) if bounds else None
    high=max(p['end_ms'] for p in phases) if bounds else None
    tools=[s for s in spans if s['kind']=='tool']
    intervals=[(max(s['start_ms'],low) if bounds else s['start_ms'],min(s['end_ms'],high) if bounds else s['end_ms']) for s in tools if s['start_ms'] is not None and s['end_ms'] is not None]
    intervals=[(a,b) for a,b in intervals if b>=a]
    durations=[s['end_ms']-s['start_ms'] if s['start_ms'] is not None and s['end_ms'] is not None else s['duration_ms'] for s in tools]
    coverage=all(p.get('coverage',data['coverage'])['tools']=='complete' for p in phases)
    metrics=[]
    def add(key,value,complete,reason,evidence):
        metrics.append({'key':key,'value':value,'status':'insufficient_data' if value is None else 'evaluated' if complete else 'partial','reason':reason,'evidence':evidence[:200]})
    refs=[{'kind':'span','id':s['id']} for s in tools]
    add('wall_ms',high-low if bounds else None,bounds,'所选阶段的起止跨度，包含阶段间隔。' if bounds else '阶段起止边界未完整记录。',[{'kind':'phase','id':p['id']} for p in phases])
    add('tool_union_ms',union(intervals) if intervals else 0 if not tools and coverage else None,coverage and len(intervals)==len(tools),'工具时间区间并集；未知区间不补零。',refs)
    add('tool_sum_ms',sum(x for x in durations if x is not None) if any(x is not None for x in durations) else 0 if not tools and coverage else None,coverage and all(x is not None for x in durations),'各调用执行用时之和；并行和嵌套可能重叠。',refs)
    state='evaluated' if all(m['status']=='evaluated' for m in metrics) else 'insufficient_data' if all(m['value'] is None for m in metrics) else 'partial'
    return {'status':state,'metrics':metrics,'findings':[],'usage':{'input_tokens':0,'output_tokens':0,'cost':0,'currency':None}}

if __name__=='__main__':print(json.dumps(evaluate(json.load(sys.stdin)),ensure_ascii=False,allow_nan=False))
