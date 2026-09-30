"""Source-specific normalization only. No latency aggregates, scores or conclusions."""
import hashlib,json
from datetime import datetime
from pathlib import Path
from .identity import upgrade_header
from .ordering import decorate

def dt(s):return datetime.fromisoformat(s.replace('Z','+00:00'))
def operation(name):return {'read':'read','write':'write','edit':'write','bash':'bash','skill':'skill','askuserquestion':'human'}.get(name.lower(),'other')
def ref(pointer):return {'source_id':'original','pointer':pointer}
def span(sid,name,phase='task',kind='tool',**extra):
    return {'id':sid,'kind':kind,'name':name,'operation':operation(name),'agent_id':'main','parent_id':None,'request_id':None,'attempt':1,'phase_id':phase,'start_ms':None,'end_ms':None,'duration_ms':None,'status':'unknown','input':None,'output':None,'usage':None,'source':ref('/'),**extra}
def envelope(path,data,run):
    return {'schema_version':'trace-hunter/1.0','run':run,'collector':{'name':'legacy-adapter','version':'1.0'},'sources':[{'id':'original','name':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}],'coverage':{'tools':'partial','model_requests':'missing'},'phases':[],'spans':[],'links':[],'evidence':[]}
def claude(path):
    d=json.loads(path.read_text());m=d['meta'];start=dt(m['started_local'])
    ms=lambda t:round((dt(t)-start).total_seconds()*1000,6) if t else None
    t=envelope(path,d,{'id':'claude-'+m['session_id'],'title':m['title'],'task_key':'supermarket-base','query':next(x['text'] for x in d['timeline'] if x['kind']=='user_message' and not x.get('is_meta')),'harness':'Claude Code','model':'Orange 5（用户标注）','status':'partial','time_basis':'导出文件的阶段边界；默认选择任务阶段 1＋2'})
    t['coverage']={'tools':'partial','model_requests':'partial'}
    for p in d['phases']:t['phases'].append({'id':str(p['id']),'name':p['name'],'purpose':'export' if p['id']==3 else 'task','start_ms':ms(p['start_local']),'end_ms':ms(p['end_local']),'coverage':{'tools':'partial' if p['id']==3 else 'complete','model_requests':'partial' if p['id']==3 else 'complete'}})
    for i,c in enumerate(d['tool_calls']):t['spans'].append(span(c['tool_use_id'],c['tool_name'],str(c['phase']),start_ms=ms(c['ts_utc']),end_ms=ms(c['ended_ts_utc']),duration_ms=c['duration_s']*1000,status='error' if c['is_error'] else 'ok',input=c['input'],output=c['result'],source=ref(f'/tool_calls/{i}')))
    tools={s['id'] for s in t['spans']}
    for i,c in enumerate(d['timeline']):
        if c['kind']!='assistant_message':continue
        u=c.get('usage');rid=c.get('request_id')
        if not rid:continue
        sid=f'message-{i}'
        usage=None if not u else {'input_tokens':sum(u.get(k,0) for k in ('input_tokens','cache_read_input_tokens','cache_creation_input_tokens')),'output_tokens':u.get('output_tokens'),'cache_read_tokens':u.get('cache_read_input_tokens'),'cache_write_tokens':u.get('cache_creation_input_tokens'),'thinking_tokens':u.get('thinking_tokens')}
        t['spans'].append(span(sid,'模型请求',str(c['phase']),kind='model',request_id=rid,end_ms=ms(c['ts_utc']),status='ok',usage=usage,output=c.get('text'),source=ref(f'/timeline/{i}')))
        for tool in c.get('tool_uses',[]):
            if tool['tool_use_id'] in tools:t['links'].append({'from':sid,'to':tool['tool_use_id'],'type':'invokes'})
    t['evidence'].append({'id':'artifact','kind':'artifact','name':'多维表格产物','status':'observed','detail':m.get('deliverable',''),'span_ids':[],'source':ref('/meta/deliverable')})
    t['evidence'].append({'id':'source-note','kind':'note','name':'采集范围','status':'observed','detail':'按用户提供的 Orange 5 标签展示；源 meta.model 保留在原文件。工具正文可能已截断；完整结果见原始 JSONL。','span_ids':[],'source':ref('/meta/caveats')})
    return decorate(t,[c['tool_use_id'] for c in d['tool_calls']])

def doubao(path):
    d=json.loads(path.read_text());m=d['manifest'];start=dt(m['actual_submission_observed_at']);ms=lambda s:round((dt(s)-start).total_seconds()*1000,6) if s else None
    sqlite='native_tool_calls' in d
    end=m.get('native_final_file_mtime') if sqlite else m.get('completed_observed_at')
    t=envelope(path,d,{'id':'doubao-'+m['conversation_id'],'title':m['query'],'task_key':'supermarket-sqlite' if sqlite else 'supermarket-base','query':m['query'],'harness':'Doubao Work','model':m['selected_model_label'],'status':'partial','time_basis':'最终回复文件落盘' if sqlite else '提交至观察到交付'})
    t['phases']=[{'id':'task','name':'任务执行','purpose':'task','start_ms':0,'end_ms':ms(end)}]
    if sqlite:
        runtime={r['runtime_id']:r for r in d['runtime_tool_calls']}
        for i,c in enumerate(d['native_tool_calls']):
            matched=c.get('runtime_match');r=runtime.get(matched,{}) if isinstance(matched,str) else (matched or {})
            if not isinstance(r,dict):r={}
            # Older exports record runtime_match as an index or a metadata object.
            if not r and c['name']=='Read' and len(runtime)==1:r=next(iter(runtime.values()))
            t['spans'].append(span(c['id'],c['name'],start_ms=ms(r.get('started_at')),end_ms=ms(r.get('ended_at')),duration_ms=r.get('duration_ms'),input=c['arguments'],output=c.get('result'),source=ref(f'/native_tool_calls/{i}')))
    else:
        native={c['tool_call_id']:c for c in d['tool_calls']};commands={c['bash_run_id']:c for c in d.get('runtime_commands',[])};seen=set()
        for i,r in enumerate(d['runtime_tool_calls']):
            n=native.get(r.get('native_tool_call_id'),{}); timing=r.get('timing',{});seen.add(r.get('native_tool_call_id'))
            command=commands.get(r['runtime_id'],{})
            t['spans'].append(span(r['runtime_id'],r['name'],start_ms=ms(timing.get('started_at')),end_ms=ms(timing.get('ended_at')),duration_ms=timing.get('duration_ms'),input=n.get('arguments') or r.get('command') or command.get('command') or r.get('input_path_metadata'),output=(n.get('native_result') or {}).get('content'),source=ref(f'/runtime_tool_calls/{i}')))
        for i,n in enumerate(d['tool_calls']):
            if n['tool_call_id'] in seen:continue
            q=n.get('timing') or {}
            t['spans'].append(span(n['tool_call_id'],n['name'],input=n['arguments'],output=(n.get('native_result') or {}).get('content'),start_ms=ms(q.get('started_at')),end_ms=ms(q.get('ended_at')),duration_ms=q.get('duration_ms'),source=ref(f'/tool_calls/{i}')))
    if sqlite:
        order=[c['id'] for c in d['native_tool_calls']]
    else:
        matched={r.get('native_tool_call_id'):r['runtime_id'] for r in d['runtime_tool_calls']}
        order=[matched.get(c['tool_call_id'],c['tool_call_id']) for c in d['tool_calls']]
    return decorate(t,order)

def normalize(path,format,query_id=None,env_id=None):
    path=Path(path)
    if format=='claude-export':return upgrade_header(claude(path),query_id,env_id)
    if format=='doubao-export':return upgrade_header(doubao(path),query_id,env_id)
    raise ValueError('Unsupported source format')
