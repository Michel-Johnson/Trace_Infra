"""Published evaluator registry and score validation over the shared task lifecycle."""
import hashlib
import json
from pathlib import Path
from sqlalchemy import text
from .contracts import SCORE,check,validate_manifest
from ..catalog import canonical
from ..database import Conflict
from ..task_lifecycle import TaskLifecycle,now,decode,LEASE_SECONDS

ROOT=Path(__file__).resolve().parents[3]

class Evaluations(TaskLifecycle):
    def bootstrap(self):
        for path in sorted((ROOT/'plugins/official').glob('*/manifest.json')):
            manifest=json.loads(path.read_text())
            if hashlib.sha256((path.parent/'evaluator.py').read_bytes()).hexdigest()!=manifest['package_digest']:
                raise ValueError('官方插件实现摘要不匹配')
            self.register(manifest,builtin=True)


    def register(self,manifest,builtin=False):
        validate_manifest(manifest)
        if self.repo.rows('SELECT version FROM extension_versions WHERE plugin_id=:id AND version=:v',{'id':manifest['plugin_id'],'v':manifest['version']}):raise Conflict('该身份已用于通用插件，请使用新版本或插件 ID')
        if manifest['plugin_id'].startswith('official.') and not builtin:raise ValueError('official 命名空间保留给随服务发布的插件')
        payload,digest=canonical(manifest);mode='builtin' if builtin else 'external'
        with self.repo.engine.begin() as db:
            db.execute(text('INSERT INTO plugin_versions(plugin_id,version,manifest_digest,manifest,execution_mode,created_at) VALUES(:id,:v,:digest,:payload,:mode,:at) ON CONFLICT(plugin_id,version) DO NOTHING'),{'id':manifest['plugin_id'],'v':manifest['version'],'digest':digest,'payload':payload,'mode':mode,'at':now()})
            row=db.execute(text('SELECT manifest_digest,execution_mode FROM plugin_versions WHERE plugin_id=:id AND version=:v'),{'id':manifest['plugin_id'],'v':manifest['version']}).mappings().one()
            if row['manifest_digest']!=digest or row['execution_mode']!=mode:raise Conflict('同名插件版本已存在且内容不同，请发布新版本')
        return self.plugin(manifest['plugin_id'],manifest['version'])


    def plugins(self):
        return [{'manifest':decode(r,'manifest'),'execution_mode':r['execution_mode'],'manifest_digest':r['manifest_digest']} for r in self.repo.rows('SELECT * FROM plugin_versions ORDER BY plugin_id,version')]


    def plugin(self,pid,version):
        rows=self.repo.rows('SELECT * FROM plugin_versions WHERE plugin_id=:id AND version=:v',{'id':pid,'v':version})
        if not rows:raise KeyError('插件版本不存在')
        r=rows[0];return {'manifest':decode(r,'manifest'),'execution_mode':r['execution_mode'],'manifest_digest':r['manifest_digest']}


    def preflight(self,row):
        data=decode(row,'snapshot');manifest=decode(row,'manifest')
        tools=[s for s in data['spans'] if s['kind']=='tool']
        coverage={
            'timing':bool(data['phases']) and all(p['start_ms'] is not None and p['end_ms'] is not None for p in data['phases']) and all(s['start_ms'] is not None and s['end_ms'] is not None for s in tools),
            'tools':all(p.get('coverage',data['coverage'])['tools']=='complete' for p in data['phases']),
            'assertions':any(e['kind']=='assertion' and e['status'] in ('passed','failed') for e in data['evidence']),
        }
        missing=[r for r in manifest['requirements'] if not coverage[r['domain']]]
        return {'blocked':any(r['on_missing']=='block' for r in missing),'missing':missing}


    def _validate_output(self,row,score):
        check(SCORE,score);manifest=decode(row,'manifest');snapshot=decode(row,'snapshot')
        definitions={m['key']:m for m in manifest['metrics']}
        if len(score['metrics'])!=len(definitions) or {m['key'] for m in score['metrics']}!=set(definitions):raise ValueError('结果必须且只能包含声明的全部指标')
        for metric in score['metrics']:
            value=metric['value'];kind=definitions[metric['key']]['type']
            if metric['status'] in ('insufficient_data','skipped'):
                if value is not None:raise ValueError('未评分指标必须为 null')
            elif value is None or not {'number':lambda:type(value) in (int,float),'boolean':lambda:type(value) is bool,'string':lambda:type(value) is str}[kind]():raise ValueError('指标值类型不匹配')
        if score['status']=='evaluated' and any(m['status']!='evaluated' for m in score['metrics']):raise ValueError('完整结果与指标状态不一致')
        if score['status'] in ('insufficient_data','skipped') and any(m['value'] is not None for m in score['metrics']):raise ValueError('未评分结果不能包含数值')
        if self.preflight(row)['blocked'] and score['status']!='insufficient_data':raise ValueError('输入不满足插件要求，必须返回 insufficient_data')
        if score['status']=='partial' and not any(m['value'] is not None for m in score['metrics']):raise ValueError('没有已知指标时应返回 insufficient_data')
        if score['usage']['cost'] and not score['usage']['currency']:raise ValueError('费用必须提供币种')
        allowed={'span':{s['id'] for s in snapshot['spans']},'evidence':{e['id'] for e in snapshot['evidence']},'phase':{p['id'] for p in snapshot['phases']}}
        for item in score['metrics']+score['findings']:
            for ref in item['evidence']:
                if ref['id'] not in allowed[ref['kind']]:raise ValueError('证据不属于本次固定输入')


    def build_result(self,row,execution,score,score_digest,finished):
        jid=row['id'];attempt=row['attempt']
        return {'schema_version':'trace-hunter/evaluation-result/1.0','job_id':jid,'attempt':attempt,'plugin_ref':{'id':row['plugin_id'],'version':row['plugin_version'],'manifest_digest':row['manifest_digest'],'package_digest':decode(row,'manifest')['package_digest']},'input_ref':{'run_id':row['run_id'],'digest':row['input_digest'],'snapshot_digest':row['snapshot_digest']},'config_digest':row['config_digest'],'scope':row['scope'],'score':score,'score_digest':score_digest,'execution':{'worker':execution['worker'],'elapsed_ms':max(0,round((finished-execution['started_at'])*1000))},'created_at':now()}

    def result_digest(self,result):return result['score_digest']
