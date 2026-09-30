"""Evaluation extension for the shared OpenAPI document."""
from .contracts import SCHEMAS, QUERY, ID, COUNT, DIGEST, obj, enum

def extend(schemas,paths):
    schemas.update(SCHEMAS)
    def ref(name):return {'$ref':'#/components/schemas/'+name}
    schemas.update({
        'EvaluationGrant':obj({'job_id':ID,'dispatch_token':ID,'expires_in':COUNT}),
        'EvaluationLease':obj({'job_id':ID,'attempt':COUNT,'lease_token':ID,'lease_seconds':COUNT}),
        'EvaluationHeartbeat':obj({'lease_seconds':COUNT}),
        'EvaluationQueryPage':obj({'items':{'type':'array','items':{'type':'object'}},'next_cursor':{'type':['integer','null']},'total':COUNT,'snapshot_digest':DIGEST}),
        'EvaluationRecord':obj({'kind':enum('span','evidence','phase','source'),'record':{'type':'object'},'run_id':ID,'input_digest':DIGEST,'snapshot_digest':DIGEST}),
        'EvaluationRecordChunk':obj({'text':{'type':'string'},'next_offset':{'type':['integer','null']},'total_chars':COUNT,'snapshot_digest':DIGEST}),
        'EvaluationInput':obj({'job_id':ID,'attempt':COUNT,'plugin':ref('PluginManifest'),'config':{'type':'object'},'input_digest':DIGEST,'snapshot_digest':DIGEST,'input':{'type':'object','description':'固定范围内的 run、environment、coverage、phases、spans、evidence、links、sources、summary、tool_rows。'},'preflight':{'type':'object'}}),
    })
    def param(name,kind='path',schema=None,required=True):return {'name':name,'in':kind,'required':required,'schema':schema or ID}
    def add(path,method,name,output,body=None,extra=(),worker=False):
        params=[param(part[1:-1]) for part in path.split('/') if part.startswith('{')]+list(extra)
        if worker:params.append(param('Authorization','header',{'type':'string','description':'Bearer dispatch token（claim）或 lease token（其他执行器调用）'}))
        op={'operationId':name,'tags':['Evaluations'],'summary':name,'parameters':params,'x-triggers-analysis':name=='createEvaluation',
            'responses':{'200':{'description':'成功','content':{'application/json':{'schema':output}}},**{str(c):{'$ref':'#/components/responses/Error'+str(c)} for c in (403,404,409,413,415,422,500)}}}
        if body is not None:op['requestBody']={'required':True,'content':{'application/json':{'schema':body}},'description':'最多 1 MiB JSON。'}
        paths.setdefault(path,{})[method]=op
    add('/api/plugin-schema','get','getPluginSchema',{'type':'object'})
    add('/api/evaluation-score-schema','get','getEvaluationScoreSchema',{'type':'object'})
    add('/api/plugins','get','listPlugins',{'type':'array','items':ref('PluginVersion')})
    add('/api/plugins','post','registerPlugin',ref('PluginVersion'),ref('PluginManifest'))
    add('/api/evaluations','get','listEvaluations',{'type':'array','items':ref('EvaluationBatch')},extra=[param('collection_id','query',required=False),param('query_id','query',required=False),param('limit','query',{'type':'integer','minimum':1,'maximum':100,'default':30},False),param('cursor','query',{**ID,'description':'上一页最后的批次 id。按 created_at DESC、id DESC 读取更早记录；返回数组，少于 limit 条表示结束。'},False)])
    add('/api/evaluations','post','createEvaluation',ref('EvaluationBatch'),ref('CreateEvaluation'))
    add('/api/evaluations/{batch_id}','get','getEvaluationBatch',ref('EvaluationBatch'))
    base='/api/evaluation-jobs/{job_id}'
    add(base,'get','getEvaluationJob',ref('EvaluationJob'))
    for action,output in [('grant','EvaluationGrant'),('cancel','EvaluationJob'),('retry','EvaluationJob')]:add(base+'/'+action,'post',action+'Evaluation',ref(output))
    add(base+'/claim','post','claimEvaluation',ref('EvaluationLease'),obj({'worker':ID}),worker=True)
    attempt=[param('attempt','query',{'type':'integer','minimum':1})]
    add(base+'/input','get','getEvaluationInput',ref('EvaluationInput'),extra=attempt,worker=True)
    add(base+'/heartbeat','post','heartbeatEvaluation',ref('EvaluationHeartbeat'),extra=attempt,worker=True)
    add(base+'/query','post','queryEvaluation',ref('EvaluationQueryPage'),QUERY,attempt,True)
    add(base+'/results','post','submitEvaluation',ref('EvaluationResult'),ref('EvaluationScore'),attempt,True)
    add(base+'/fail','post','failEvaluation',ref('EvaluationJob'),obj({'error':{'type':'string','maxLength':2000}}),attempt,True)
    add(base+'/records/{kind}/{record_id}','get','getEvaluationEvidence',ref('EvaluationRecord'))
    add(base+'/record-read','get','readEvaluationRecord',ref('EvaluationRecordChunk'),extra=attempt+[param('kind','query',enum('span','evidence','phase','source')),param('record_id','query'),param('offset','query',COUNT,False),param('limit','query',{'type':'integer','minimum':1,'maximum':64000},False)],worker=True)
