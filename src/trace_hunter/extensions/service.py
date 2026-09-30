"""General registry and typed computations; UI contributions never enter the queue."""
import copy
import hashlib
import json
import time
from pathlib import Path
from sqlalchemy import text
from .contracts import ROOT,CREATE,OUTPUT,check,validate_manifest,validate_facets,entity_key,project_evaluator,classification_targets
from ..catalog import canonical
from ..database import Conflict
from ..task_tables import CONTRIBUTION_TABLES
from ..task_lifecycle import TaskLifecycle,decode,now
from ..evaluations.service import Evaluations

BROWSER_REFS={'trace.grid','trace.list','trace.operations','trace.facets'}

class Extensions:
    def __init__(self,store,evaluations=None):
        self.store=store;self.repo=store.repository;self.evaluations=evaluations or Evaluations(store)
        self.tasks=ContributionTasks(self)

    def bootstrap(self):
        for path in (ROOT/'plugins/extensions').glob('*/manifest.json'):
            manifest=json.loads(path.read_text());package=json.loads((path.parent/'package.json').read_text())
            for filename,digest in package['files'].items():
                source=(ROOT/filename).resolve()
                if not source.is_relative_to(ROOT) or hashlib.sha256(source.read_bytes()).hexdigest()!=digest:raise ValueError('插件包文件摘要不匹配')
            if canonical(package['files'])[1]!=manifest['package_digest']:raise ValueError('插件包摘要不匹配')
            self.register(manifest,builtin=True)

    def native(self,pid,version):
        rows=self.repo.rows('SELECT * FROM extension_versions WHERE plugin_id=:id AND version=:v',{'id':pid,'v':version})
        if not rows:raise KeyError('通用插件版本不存在')
        row=rows[0];return {'manifest':decode(row,'manifest'),'manifest_digest':row['manifest_digest'],'origin':row['origin']}

    def register(self,value,builtin=False):
        validate_manifest(value)
        if value['plugin_id'].startswith('official.') and not builtin:raise ValueError('official 命名空间保留给官方插件')
        if any(p['manifest']['plugin_id']==value['plugin_id'] and p['manifest']['version']==value['version'] for p in self.evaluations.plugins()):raise Conflict('该身份已用于兼容评分插件，请选择新版本或插件 ID')
        payload,digest=canonical(value);origin='builtin' if builtin else 'external'
        with self.repo.engine.begin() as db:
            db.execute(text('INSERT INTO extension_versions(plugin_id,version,manifest_digest,manifest,origin,created_at) VALUES(:id,:v,:digest,:payload,:origin,:at) ON CONFLICT(plugin_id,version) DO NOTHING'),{'id':value['plugin_id'],'v':value['version'],'digest':digest,'payload':payload,'origin':origin,'at':now()})
            old=db.execute(text('SELECT manifest_digest,origin FROM extension_versions WHERE plugin_id=:id AND version=:v'),{'id':value['plugin_id'],'v':value['version']}).mappings().one()
            if old['manifest_digest']!=digest or old['origin']!=origin:raise Conflict('同名插件版本内容不同，请发布新版本')
            for contribution in value['contributes']:
                if contribution['trigger']!='explicit':continue
                definition={**value,'contribution_id':contribution['id'],'contribution':contribution,'title':contribution['title'],
                    'manifest_digest':digest,'trace_versions':[v for v in contribution['consumes'] if v in ('trace-hunter/1.0','trace-hunter/1.1')],
                    'config_schema':contribution['config_schema'],'default_config':contribution['default_config'],'metrics':contribution.get('metrics',[]),'requirements':contribution.get('requirements',[])}
                encoded,definition_digest=canonical(definition)
                db.execute(text('INSERT INTO plugin_handlers(plugin_id,version,manifest_digest,manifest,execution_mode,created_at) VALUES(:id,:v,:digest,:payload,:mode,:at) ON CONFLICT(plugin_id,version) DO NOTHING'),
                    {'id':self.tasks.handler_id(value['plugin_id'],contribution['id']),'v':value['version'],'digest':definition_digest,'payload':encoded,'mode':'builtin' if builtin else 'external','at':now()})
        return self.describe(self.native(value['plugin_id'],value['version']))

    def describe(self,entry):
        native=entry['manifest'];legacy=entry.get('origin')=='evaluation_v1';modes={}
        for c in native['contributes']:
            if legacy:mode=entry['execution_mode']
            elif c['implementation']['host']=='browser':mode='browser' if entry['origin']=='builtin' and c['implementation']['ref'] in BROWSER_REFS else 'unavailable'
            elif c['trigger']=='explicit':mode='builtin' if entry['origin']=='builtin' else 'external'
            else:mode='unavailable'
            modes[c['id']]=mode
        return {'manifest':native,'manifest_digest':entry['manifest_digest'],'source':'evaluation_v1' if legacy else 'native','runtimes':modes}

    def catalog(self):
        result=[self.describe({'manifest':decode(r,'manifest'),'manifest_digest':r['manifest_digest'],'origin':r['origin']}) for r in self.repo.rows('SELECT * FROM extension_versions ORDER BY plugin_id,version')]
        for legacy in self.evaluations.plugins():
            result.append(self.describe({**legacy,'manifest':project_evaluator(legacy['manifest']),'origin':'evaluation_v1'}))
        return result

class ContributionTasks(TaskLifecycle):
    tables = CONTRIBUTION_TABLES
    def __init__(self,registry):
        super().__init__(registry.store);self.registry=registry

    def plugins(self):return self.registry.catalog()
    def register(self,value):return self.registry.register(value)

    @staticmethod
    def handler_id(pid,cid):return hashlib.sha256(json.dumps([pid,cid],separators=(',',':')).encode()).hexdigest()

    def plugin(self,pid,version):
        rows=self.repo.rows(f'SELECT * FROM {self.tables.plugins} WHERE plugin_id=:id AND version=:v',{'id':pid,'v':version})
        if not rows:raise KeyError('可执行贡献点不存在')
        r=rows[0];return {'manifest':decode(r,'manifest'),'manifest_digest':r['manifest_digest'],'execution_mode':r['execution_mode']}

    def create(self,request):
        check(CREATE,request);entry=self.registry.native(request['plugin_id'],request['plugin_version'])
        contributions=[c for c in entry['manifest']['contributes'] if c['id']==request['contribution_id']]
        if not contributions:raise KeyError('贡献点不存在')
        c=contributions[0]
        if c['trigger']!='explicit':raise ValueError('渲染与即时筛选不会创建执行任务')
        if 'run' not in c['scopes']:raise ValueError('当前任务执行入口支持 run 范围')
        if not set(c['consumes']).issubset({'trace-hunter/1.0','trace-hunter/1.1'}):raise ValueError('当前计算入口只接收轨迹快照；尚未配置派生输入依赖')
        normalized={k:v for k,v in request.items() if k!='contribution_id'}
        normalized['plugin_id']=self.handler_id(request['plugin_id'],request['contribution_id'])
        return super().create(normalized)

    def job(self,jid):
        result=super().job(jid);definition=self.plugin(result['plugin_id'],result['plugin_version'])['manifest']
        result.update(plugin_id=definition['plugin_id'],contribution_id=definition['contribution_id'],output_kind=definition['contribution']['output_kind'])
        return result

    def batch(self,bid,query_id=None):
        result=super().batch(bid,query_id=query_id);definition=self.plugin(result['plugin_id'],result['plugin_version'])['manifest']
        result.update(plugin_id=definition['plugin_id'],contribution_id=definition['contribution_id'],output_kind=definition['contribution']['output_kind'])
        return result

    def input_ref(self,row):
        return {'document_id':row['run_id'],'document_digest':row['input_digest'],'selection_digest':row['snapshot_digest']}

    def context(self,jid,attempt,token):
        value=super().context(jid,attempt,token);row=self._job(jid)
        value['input_ref']=self.input_ref(row);value['output_schema']=OUTPUT
        return value

    def preflight(self,row):return self.registry.evaluations.preflight(row)

    def _validate_output(self,row,output):
        check(OUTPUT,output);definition=decode(row,'manifest')
        if output['kind']!=definition['contribution']['output_kind']:raise ValueError('输出类型与贡献点声明不同')
        if output['kind']=='evaluation':return self.registry.evaluations._validate_output(row,output['data'])
        snapshot=decode(row,'snapshot')
        allowed={(row['run_id'],row['input_digest'],'span',s['id']) for s in classification_targets(definition,snapshot)}
        validate_facets(output['data'],self.input_ref(row),allowed)
        usage=output['usage']
        if usage['cost'] and not usage['currency']:raise ValueError('费用必须提供币种')

    def result_digest(self,result):return result['output_digest']

    def build_result(self,row,execution,output,digest,finished):
        definition=decode(row,'manifest')
        return {'schema_version':'trace-hunter/plugin-result/1.0','job_id':row['id'],'attempt':row['attempt'],
            'plugin_ref':{'id':definition['plugin_id'],'version':row['plugin_version'],'manifest_digest':definition['manifest_digest'],'package_digest':definition['package_digest']},
            'contribution_id':definition['contribution_id'],'input_ref':self.input_ref(row),'config_digest':row['config_digest'],'scope':row['scope'],
            'output':output,'output_digest':digest,'execution':{'worker':execution['worker'],'elapsed_ms':max(0,round((finished-execution['started_at'])*1000))},'created_at':now()}
