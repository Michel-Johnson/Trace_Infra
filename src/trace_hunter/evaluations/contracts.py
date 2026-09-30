"""Published evaluator schemas shared by HTTP, workers and plugin authors."""
import json
from jsonschema import Draft202012Validator


def obj(props, required=None):
    return {'type':'object','properties':props,'required':list(props) if required is None else required,'additionalProperties':False}
def arr(item, maximum=200): return {'type':'array','items':item,'maxItems':maximum}
def enum(*values): return {'enum':list(values)}
TEXT={'type':'string','maxLength':10000}
ID={'type':'string','minLength':1,'maxLength':256}
DIGEST={'type':'string','pattern':'^[a-f0-9]{64}$'}
COUNT={'type':'integer','minimum':0}
NULL_NUMBER={'type':['number','null'],'minimum':0}
STATUS=enum('evaluated','partial','insufficient_data','skipped')
EVIDENCE=obj({'kind':enum('span','evidence','phase'),'id':ID})
METRIC_DEF=obj({'key':ID,'title':ID,'type':enum('number','boolean','string'),'unit':{'type':['string','null']},'direction':enum('higher','lower','none'),'aggregation':enum('none','mean','sum','pass_rate','distribution')})
MANIFEST=obj({
    'schema_version':{'const':'trace-hunter/plugin/1.0'},'plugin_id':{'type':'string','pattern':'^[a-z][a-z0-9.-]{2,95}$'},
    'version':{'type':'string','pattern':'^[0-9]+\\.[0-9]+\\.[0-9]+$'},'title':ID,'description':TEXT,
    'package_digest':DIGEST,'trace_versions':{'type':'array','items':enum('trace-hunter/1.0','trace-hunter/1.1'),'minItems':1,'uniqueItems':True},
    'entrypoint':obj({'kind':enum('external_program','agent_skill'),'ref':ID}),
    'scopes':{'type':'array','items':{'const':'run'},'minItems':1,'maxItems':1},
    'requirements':arr(obj({'domain':enum('timing','tools','assertions'),'on_missing':enum('block','allow_partial')}),10),
    'config_schema':{'type':'object'},'default_config':{'type':'object'},'metrics':{'type':'array','items':METRIC_DEF,'minItems':1,'maxItems':50},
})
SCORE=obj({'status':STATUS,'metrics':arr(obj({'key':ID,'value':{'type':['number','boolean','string','null']},'status':STATUS,'reason':TEXT,'evidence':arr(EVIDENCE,200)}),50),
    'findings':arr(obj({'code':ID,'severity':enum('info','warning','error'),'message':TEXT,'repair_suggestion':{'type':['string','null'],'maxLength':10000},'evidence':arr(EVIDENCE,200)})),
    'usage':obj({'input_tokens':{'type':['integer','null'],'minimum':0},'output_tokens':{'type':['integer','null'],'minimum':0},'cost':NULL_NUMBER,'currency':{'type':['string','null'],'maxLength':8}})})
CREATE=obj({'plugin_id':ID,'plugin_version':ID,'run_ids':{'type':'array','items':ID,'minItems':1,'maxItems':50,'uniqueItems':True},'scope':enum('task','all'),'config':{'type':'object'},'collection_id':{'type':['string','null'],'maxLength':256},'request_key':{'type':'string','minLength':8,'maxLength':128}})
QUERY=obj({'kind':enum('spans','evidence','phases','links','sources'),'cursor':COUNT,'limit':{'type':'integer','minimum':1,'maximum':100},'ids':arr(ID,100)},['kind'])
PLUGIN=obj({'manifest':MANIFEST,'execution_mode':enum('builtin','external'),'manifest_digest':DIGEST})
RESULT=obj({'schema_version':{'const':'trace-hunter/evaluation-result/1.0'},'job_id':ID,'attempt':COUNT,
    'plugin_ref':obj({'id':ID,'version':ID,'manifest_digest':DIGEST,'package_digest':DIGEST}),
    'input_ref':obj({'run_id':ID,'digest':DIGEST,'snapshot_digest':DIGEST}),
    'config_digest':DIGEST,'scope':enum('task','all'),'score':SCORE,'score_digest':DIGEST,
    'execution':obj({'worker':ID,'elapsed_ms':COUNT}),'created_at':ID})
ATTEMPT=obj({'attempt':COUNT,'worker':ID,'state':enum('running','completed','failed','cancelled','expired'),
    'started_at':{'type':'number'},'finished_at':{'type':['number','null']},'error':{'type':['string','null']}})
JOB=obj({**{k:ID for k in ('id','batch_id','run_id','query_id','run_title','model','harness','created_at','updated_at','plugin_id','plugin_version')},
    'state':enum('queued','running','completed','failed','cancelled'),'attempt':COUNT,'execution_mode':enum('builtin','external'),
    'scope':enum('task','all'),'config':{'type':'object'},'input_digest':DIGEST,'snapshot_digest':DIGEST,
    'attempts':{'type':'array','items':ATTEMPT},'result':{'anyOf':[RESULT,{'type':'null'}]}})
BATCH=obj({**{k:ID for k in ('id','created_at','plugin_id','plugin_version','plugin_title')},'collection_id':{'type':['string','null']},
    'scope':enum('task','all'),'config':{'type':'object'},'jobs':arr(JOB,50),'counts':obj({s:COUNT for s in ('queued','running','completed','failed','cancelled')})})
SCHEMAS={'PluginManifest':MANIFEST,'EvaluationScore':SCORE,'CreateEvaluation':CREATE,'EvaluationQuery':QUERY,'PluginVersion':PLUGIN,'EvaluationResult':RESULT,'EvaluationJob':JOB,'EvaluationBatch':BATCH}

def check(schema,value):
    try:
        raw=json.dumps(value,allow_nan=False)
        if len(raw.encode())>1024*1024: raise ValueError('评测参数超过 1 MiB')
        Draft202012Validator(schema).validate(value)
    except Exception as exc:
        if isinstance(exc,ValueError) and str(exc)=='评测参数超过 1 MiB':raise
        raise ValueError('评测数据不符合 Schema') from None
    return value

def validate_manifest(value):
    check(MANIFEST,value)
    def local_refs(item):
        if isinstance(item,dict):
            for key,child in item.items():
                if key in ('$ref','$dynamicRef') and (not isinstance(child,str) or not child.startswith('#')):
                    raise ValueError('配置 Schema 仅支持本地引用')
                local_refs(child)
        elif isinstance(item,list):
            for child in item:local_refs(child)
    local_refs(value['config_schema'])
    try:Draft202012Validator.check_schema(value['config_schema'])
    except Exception:raise ValueError('配置 Schema 本身无效') from None
    check(value['config_schema'],value['default_config'])
    keys=[m['key'] for m in value['metrics']]
    if len(keys)!=len(set(keys)):raise ValueError('指标 key 重复')
    for metric in value['metrics']:
        allowed={'number':{'none','mean','sum','distribution'},'boolean':{'none','pass_rate','distribution'},'string':{'none','distribution'}}
        if metric['aggregation'] not in allowed[metric['type']]:raise ValueError('指标类型与聚合方法不匹配')
    return value
