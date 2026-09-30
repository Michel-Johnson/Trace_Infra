"""Shared immutable-input task lifecycle. Concrete services supply registry and outputs."""
import copy
import hashlib
import json
import secrets
import time
import uuid
from datetime import datetime, timezone
from sqlalchemy import text
from .catalog import canonical
from .database import Conflict
from .task_tables import EVALUATION_TABLES
from .evaluations.contracts import CREATE,QUERY,check

LEASE_SECONDS=120

def now():return datetime.now(timezone.utc).isoformat()
def sha(value):return hashlib.sha256(value.encode()).hexdigest()
def decode(row,key):return json.loads(row[key])

class TaskLifecycle:
    tables = EVALUATION_TABLES

    def __init__(self, store):
        self.store = store
        self.repo = store.repository

    def create(self,request):
        check(CREATE,request)
        plugin=self.plugin(request['plugin_id'],request['plugin_version']);manifest=plugin['manifest']
        check(manifest['config_schema'],request['config'])
        excluded=None
        if request['collection_id']=='__unassigned__':
            excluded={r['query_id'] for r in self.repo.rows('SELECT query_id FROM collection_cases')}
            allowed=None
        elif request['collection_id']:
            if not self.repo.rows('SELECT id FROM collections WHERE id=:id',{'id':request['collection_id']}):raise KeyError('集合不存在')
            allowed={r['query_id'] for r in self.repo.rows('SELECT query_id FROM collection_cases WHERE collection_id=:id',{'id':request['collection_id']})}
        else:allowed=None
        snapshots=[]
        for rid in request['run_ids']:
            bundle=self.store.get(rid)
            if bundle['trace']['schema_version'] not in manifest['trace_versions']:raise ValueError('插件不支持这条轨迹的协议版本')
            if (allowed is not None and bundle['run']['query_id'] not in allowed) or (excluded is not None and bundle['run']['query_id'] in excluded):raise ValueError('运行不属于所选集合')
            if request['scope']=='all':bundle=self.store.compare([rid],True)['runs'][0]
            phase_ids=set(bundle['view']['scope']);trace=bundle['trace']
            spans=[s for s in trace['spans'] if s['phase_id'] in phase_ids];span_ids={s['id'] for s in spans}
            evidence=[e for e in trace['evidence'] if not e['span_ids'] or set(e['span_ids']).issubset(span_ids)]
            snapshot={'schema_version':trace['schema_version'],'run':bundle['run'],'environment':bundle['environment'],'coverage':trace['coverage'],'phases':[p for p in trace['phases'] if p['id'] in phase_ids],'spans':spans,'evidence':evidence,'links':[link for link in trace['links'] if link['from'] in span_ids and link['to'] in span_ids],'sources':trace['sources'],'summary':bundle['view']['summary'],'tool_rows':bundle['view']['rows']}
            payload,digest=canonical(snapshot)
            if len(payload.encode())>20*1024*1024:raise ValueError('评测输入快照超过 20 MiB')
            snapshots.append((bundle,payload,digest))
        request_payload,request_digest=canonical(request);config,config_digest=canonical(request['config'])
        batch_id='eval-'+uuid.uuid4().hex;at=now()
        with self.repo.engine.begin() as db:
            inserted=db.execute(text(f'INSERT INTO {self.tables.batches}(id,created_at,plugin_id,plugin_version,collection_id,scope,config,config_digest,request_key,request_digest) VALUES(:id,:at,:pid,:v,:cid,:scope,:config,:cd,:rk,:rd) ON CONFLICT(request_key) DO NOTHING'),{'id':batch_id,'at':at,'pid':request['plugin_id'],'v':request['plugin_version'],'cid':request['collection_id'],'scope':request['scope'],'config':config,'cd':config_digest,'rk':request['request_key'],'rd':request_digest}).rowcount
            if not inserted:
                existing=db.execute(text(f'SELECT id,request_digest FROM {self.tables.batches} WHERE request_key=:rk'),{'rk':request['request_key']}).mappings().one()
                if existing['request_digest']!=request_digest:raise Conflict('请求标识已用于另一组评测参数')
                batch_id=existing['id']
            else:
                for index,(bundle,payload,digest) in enumerate(snapshots):
                    db.execute(text(f"INSERT INTO {self.tables.jobs}(id,batch_id,run_id,query_id,input_digest,snapshot_digest,snapshot,state,attempt,created_at,updated_at,position) VALUES(:id,:batch,:rid,:qid,:digest,:sd,:snapshot,'queued',0,:at,:at,:position)"),{'id':'job-'+uuid.uuid4().hex,'batch':batch_id,'rid':bundle['run']['id'],'qid':bundle['run']['query_id'],'digest':bundle['digest'],'sd':digest,'snapshot':payload,'at':at,'position':index})
        return self.batch(batch_id)


    def _job(self,jid,db=None):
        sql=f'SELECT j.*,b.plugin_id,b.plugin_version,b.config,b.config_digest,b.scope,b.collection_id,p.manifest,p.manifest_digest,p.execution_mode FROM {self.tables.jobs} j JOIN {self.tables.batches} b ON b.id=j.batch_id JOIN {self.tables.plugins} p ON p.plugin_id=b.plugin_id AND p.version=b.plugin_version WHERE j.id=:id'
        if db is not None and self.repo.postgres:sql+=' FOR UPDATE OF j'
        rows=(db.execute(text(sql),{'id':jid}).mappings().all() if db is not None else self.repo.rows(sql,{'id':jid}))
        if not rows:raise KeyError('评测任务不存在')
        return rows[0]


    def job(self,jid):
        row=self._job(jid);snapshot=decode(row,'snapshot')
        attempts=self.repo.rows(f'SELECT attempt,worker,state,started_at,finished_at,error FROM {self.tables.attempts} WHERE job_id=:id ORDER BY attempt',{'id':jid})
        result=self.repo.rows(f'SELECT payload,digest FROM {self.tables.results} WHERE job_id=:id AND attempt=:attempt',{'id':jid,'attempt':row['attempt']})
        return {'id':jid,'batch_id':row['batch_id'],'run_id':row['run_id'],'query_id':row['query_id'],'run_title':snapshot['run']['title'],'model':snapshot['run']['model'],'harness':snapshot['run']['harness'],'state':row['state'],'attempt':row['attempt'],'created_at':row['created_at'],'updated_at':row['updated_at'],'plugin_id':row['plugin_id'],'plugin_version':row['plugin_version'],'execution_mode':row['execution_mode'],'scope':row['scope'],'config':decode(row,'config'),'input_digest':row['input_digest'],'snapshot_digest':row['snapshot_digest'],'attempts':[dict(a) for a in attempts],'result':decode(result[0],'payload') if result else None}


    def batches(self,collection_id=None,query_id=None,limit=30,cursor=None):
        if not 1<=limit<=100:raise ValueError('limit 必须为 1–100')
        where=[];params={'limit':limit}
        if cursor is not None:
            if not isinstance(cursor,str) or not 1<=len(cursor)<=256:raise ValueError('cursor 必须是上一页最后的批次 ID')
            anchor=self.repo.rows(f'SELECT id,created_at FROM {self.tables.batches} WHERE id=:cursor',{'cursor':cursor})
            if not anchor:raise ValueError('cursor 批次不存在')
            where.append('(b.created_at<:cursor_at OR (b.created_at=:cursor_at AND b.id<:cursor_id))')
            params.update(cursor_at=anchor[0]['created_at'],cursor_id=anchor[0]['id'])
        if collection_id:where.append('b.collection_id=:cid');params['cid']=collection_id
        if query_id:where.append(f'EXISTS(SELECT 1 FROM {self.tables.jobs} j WHERE j.batch_id=b.id AND j.query_id=:qid)');params['qid']=query_id
        sql=f'SELECT b.id FROM {self.tables.batches} b'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY b.created_at DESC,b.id DESC LIMIT :limit'
        batches=[self.batch(row['id'],query_id=query_id) for row in self.repo.rows(sql,params)]
        if query_id:
            for batch in batches:
                batch['jobs']=[j for j in batch['jobs'] if j['query_id']==query_id]
                batch['counts']={state:sum(j['state']==state for j in batch['jobs']) for state in batch['counts']}
        return batches


    def batch(self,bid,query_id=None):
        rows=self.repo.rows(f'SELECT * FROM {self.tables.batches} WHERE id=:id',{'id':bid})
        if not rows:raise KeyError('评测批次不存在')
        row=rows[0]
        jobs_sql=f'SELECT id FROM {self.tables.jobs} WHERE batch_id=:id'+(' AND query_id=:qid' if query_id else '')+' ORDER BY position'
        jobs=[self.job(j['id']) for j in self.repo.rows(jobs_sql,{'id':bid,'qid':query_id})]
        manifest=self.plugin(row['plugin_id'],row['plugin_version'])['manifest']
        return {'id':bid,'created_at':row['created_at'],'plugin_id':row['plugin_id'],'plugin_version':row['plugin_version'],'plugin_title':manifest['title'],'collection_id':row['collection_id'],'scope':row['scope'],'config':decode(row,'config'),'jobs':jobs,'counts':{state:sum(j['state']==state for j in jobs) for state in ('queued','running','completed','failed','cancelled')}}


    def grant(self,jid):
        token=secrets.token_urlsafe(32)
        with self.repo.engine.begin() as db:
            row=self._job(jid,db)
            if row['execution_mode']!='external' or row['state']!='queued':raise Conflict('仅待执行的外部插件任务可以生成接入凭据')
            changed=db.execute(text(f"UPDATE {self.tables.jobs} SET grant_hash=:hash,grant_expires=:until WHERE id=:id AND state='queued'"),{'id':jid,'hash':sha(token),'until':time.time()+3600}).rowcount
            if changed!=1:raise Conflict('任务状态已变化')
        return {'job_id':jid,'dispatch_token':token,'expires_in':3600}


    def claim(self,jid,worker,dispatch_token=None,builtin=False):
        if not isinstance(worker,str) or not 1<=len(worker)<=128:raise ValueError('worker 名称无效')
        token=secrets.token_urlsafe(32);ts=time.time()
        with self.repo.engine.begin() as db:
            row=self._job(jid,db)
            if builtin:
                if row['execution_mode']!='builtin':raise PermissionError('该任务需要外部执行器')
            elif row['execution_mode']!='external' or not row['grant_hash'] or not secrets.compare_digest(row['grant_hash'],sha(dispatch_token or '')) or (row['grant_expires'] or 0)<=ts:
                raise PermissionError('接入凭据无效或已过期')
            if row['state']!='queued':raise Conflict('任务已被领取或不在队列中')
            attempt=row['attempt']+1
            changed=db.execute(text(f"UPDATE {self.tables.jobs} SET state='running',attempt=:attempt,updated_at=:at,grant_hash=NULL,grant_expires=NULL WHERE id=:id AND state='queued' AND attempt=:previous"),{'id':jid,'attempt':attempt,'previous':row['attempt'],'at':now()}).rowcount
            if changed!=1:raise Conflict('任务已被其他执行器领取')
            db.execute(text(f"INSERT INTO {self.tables.attempts}(job_id,attempt,worker,token_hash,lease_until,started_at,state) VALUES(:id,:attempt,:worker,:hash,:until,:start,'running')"),{'id':jid,'attempt':attempt,'worker':worker,'hash':sha(token),'until':ts+LEASE_SECONDS,'start':ts})
        return {'job_id':jid,'attempt':attempt,'lease_token':token,'lease_seconds':LEASE_SECONDS}


    def _auth(self,jid,attempt,token,db=None,completed=False):
        row=self._job(jid,db)
        sql=f'SELECT * FROM {self.tables.attempts} WHERE job_id=:id AND attempt=:attempt';args={'id':jid,'attempt':attempt}
        attempts=db.execute(text(sql),args).mappings().all() if db is not None else self.repo.rows(sql,args)
        if not attempts or row['attempt']!=attempt or not secrets.compare_digest(attempts[0]['token_hash'],sha(token or '')):raise PermissionError('评测凭据不属于当前任务或尝试')
        if completed and row['state']=='completed':return row,attempts[0]
        if row['state']!='running' or attempts[0]['lease_until']<=time.time():raise Conflict('任务已结束、取消或租约过期')
        return row,attempts[0]


    def context(self,jid,attempt,token):
        row,_=self._auth(jid,attempt,token)
        return {'job_id':jid,'attempt':attempt,'plugin':decode(row,'manifest'),'config':decode(row,'config'),'input_digest':row['input_digest'],'snapshot_digest':row['snapshot_digest'],'input':decode(row,'snapshot'),'preflight':self.preflight(row)}


    def heartbeat(self,jid,attempt,token):
        with self.repo.engine.begin() as db:
            self._auth(jid,attempt,token,db)
            db.execute(text(f'UPDATE {self.tables.attempts} SET lease_until=:until WHERE job_id=:id AND attempt=:attempt'),{'until':time.time()+LEASE_SECONDS,'id':jid,'attempt':attempt})
        return {'lease_seconds':LEASE_SECONDS}


    def query(self,jid,attempt,token,request):
        check(QUERY,request);row,_=self._auth(jid,attempt,token)
        values=decode(row,'snapshot')[request['kind']]
        if request['kind']=='links' and request.get('ids'):raise ValueError('links 没有独立 ID，请分页查询')
        if request.get('ids'):values=[v for v in values if v['id'] in request['ids']]
        offset=request.get('cursor',0);items=[];size=0
        for value in values[offset:offset+request.get('limit',50)]:
            raw=json.dumps(value,ensure_ascii=False)
            if len(raw.encode())>256000:value={'id':value['id'],'content_omitted':True,'reason':'use record_read for chunked content'}
            weight=len(json.dumps(value,ensure_ascii=False).encode())
            if items and size+weight>256000:break
            items.append(value);size+=weight
        following=offset+len(items)
        return {'items':items,'next_cursor':following if following<len(values) else None,'total':len(values),'snapshot_digest':row['snapshot_digest']}


    def record(self,jid,kind,rid):
        if kind not in ('span','evidence','phase','source'):raise ValueError('无效的记录类型')
        row=self._job(jid);snapshot=decode(row,'snapshot')
        field={'span':'spans','evidence':'evidence','phase':'phases','source':'sources'}[kind]
        for record in snapshot[field]:
            if record['id']==rid:return {'kind':kind,'record':record,'run_id':row['run_id'],'input_digest':row['input_digest'],'snapshot_digest':row['snapshot_digest']}
        raise KeyError('证据不在本次评测的输入范围内')


    def submit(self,jid,attempt,token,score):
        score=copy.deepcopy(score)
        with self.repo.engine.begin() as db:
            row,execution=self._auth(jid,attempt,token,db,completed=True)
            self._validate_output(row,score);score_payload,score_digest=canonical(score)
            existing=db.execute(text(f'SELECT payload FROM {self.tables.results} WHERE job_id=:id AND attempt=:attempt'),{'id':jid,'attempt':attempt}).mappings().all()
            if existing:
                if self.result_digest(decode(existing[0],'payload'))!=score_digest:raise Conflict('本次尝试已提交不同的结果')
                return decode(existing[0],'payload')
            finished=time.time()
            if execution['lease_until']<=finished:raise Conflict('执行器租约已过期')
            changed=db.execute(text(f"UPDATE {self.tables.jobs} SET state='completed',updated_at=:at WHERE id=:id AND state='running' AND attempt=:attempt"),{'id':jid,'attempt':attempt,'at':now()}).rowcount
            if changed!=1:raise Conflict('任务已被取消或结束')
            result=self.build_result(row,execution,score,score_digest,finished)
            payload,digest=canonical(result)
            db.execute(text(f'INSERT INTO {self.tables.results}(job_id,attempt,payload,digest,created_at) VALUES(:id,:attempt,:payload,:digest,:at)'),{'id':jid,'attempt':attempt,'payload':payload,'digest':digest,'at':result['created_at']})
            db.execute(text(f"UPDATE {self.tables.attempts} SET state='completed',finished_at=:end WHERE job_id=:id AND attempt=:attempt"),{'id':jid,'attempt':attempt,'end':finished})
        return result


    def fail(self,jid,attempt,token,error):
        with self.repo.engine.begin() as db:
            self._auth(jid,attempt,token,db)
            changed=db.execute(text(f"UPDATE {self.tables.jobs} SET state='failed',updated_at=:at WHERE id=:id AND state='running' AND attempt=:attempt"),{'id':jid,'attempt':attempt,'at':now()}).rowcount
            if changed!=1:raise Conflict('任务已结束')
            db.execute(text(f"UPDATE {self.tables.attempts} SET state='failed',finished_at=:end,error=:error WHERE job_id=:id AND attempt=:attempt"),{'id':jid,'attempt':attempt,'end':time.time(),'error':str(error)[:2000]})
        return self.job(jid)


    def cancel(self,jid):
        with self.repo.engine.begin() as db:
            row=self._job(jid,db)
            if row['state'] not in ('queued','running'):raise Conflict('只能取消排队或执行中的任务')
            changed=db.execute(text(f"UPDATE {self.tables.jobs} SET state='cancelled',updated_at=:at,grant_hash=NULL WHERE id=:id AND state IN ('queued','running') AND attempt=:attempt"),{'id':jid,'attempt':row['attempt'],'at':now()}).rowcount
            if changed!=1:raise Conflict('任务状态已变化')
            db.execute(text(f"UPDATE {self.tables.attempts} SET state='cancelled',finished_at=:end WHERE job_id=:id AND attempt=:attempt AND state='running'"),{'id':jid,'attempt':row['attempt'],'end':time.time()})
        return self.job(jid)


    def retry(self,jid):
        with self.repo.engine.begin() as db:
            row=self._job(jid,db)
            if row['state'] not in ('failed','cancelled'):raise Conflict('只有失败或取消的任务可以重试；已完成请重新发起评测')
            changed=db.execute(text(f"UPDATE {self.tables.jobs} SET state='queued',updated_at=:at,grant_hash=NULL,grant_expires=NULL WHERE id=:id AND state IN ('failed','cancelled') AND attempt=:attempt"),{'id':jid,'attempt':row['attempt'],'at':now()}).rowcount
            if changed!=1:raise Conflict('任务状态已变化')
        return self.job(jid)


    def expire(self):
        for r in self.repo.rows(f"SELECT j.id,j.attempt FROM {self.tables.jobs} j JOIN {self.tables.attempts} a ON a.job_id=j.id AND a.attempt=j.attempt WHERE j.state='running' AND a.lease_until<:ts",{'ts':time.time()}):
            with self.repo.engine.begin() as db:
                changed=db.execute(text(f"UPDATE {self.tables.jobs} SET state='failed',updated_at=:at WHERE id=:id AND state='running' AND attempt=:attempt AND EXISTS(SELECT 1 FROM {self.tables.attempts} a WHERE a.job_id=:id AND a.attempt=:attempt AND a.lease_until<:ts)"),{'id':r['id'],'attempt':r['attempt'],'at':now(),'ts':time.time()}).rowcount
                if changed:db.execute(text(f"UPDATE {self.tables.attempts} SET state='expired',finished_at=:ts,error='执行器租约过期，可重试' WHERE job_id=:id AND attempt=:attempt"),{'id':r['id'],'attempt':r['attempt'],'ts':time.time()})
