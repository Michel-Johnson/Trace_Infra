"""General extension routes share the task transport contract with legacy evaluators."""
import copy
from .contracts import MANIFEST,FACETS,OUTPUT,CREATE,ID,DIGEST,COUNT,obj,enum
from ..evaluations.contracts import JOB,BATCH,RESULT

def extend(schemas,paths):
    ref=lambda name:{'$ref':'#/components/schemas/'+name}
    schemas['ExtensionManifest']=MANIFEST
    schemas['FacetSet']=FACETS
    schemas['PluginOutput']=OUTPUT
    schemas['CreatePluginRun']=CREATE
    schemas['ExtensionVersion']=obj({'manifest':ref('ExtensionManifest'),'manifest_digest':DIGEST,'source':enum('native','evaluation_v1'),'runtimes':{'type':'object','additionalProperties':enum('browser','builtin','external','unavailable')}})
    schemas['PluginArtifact']=obj({'schema_version':{'const':'trace-hunter/plugin-result/1.0'},'job_id':ID,'attempt':COUNT,'plugin_ref':RESULT['properties']['plugin_ref'],
        'contribution_id':ID,'input_ref':FACETS['properties']['input_refs']['items'],'config_digest':DIGEST,'scope':enum('task','all'),'output':ref('PluginOutput'),'output_digest':DIGEST,'execution':RESULT['properties']['execution'],'created_at':ID})
    schemas['PluginJob']=obj({**JOB['properties'],'contribution_id':ID,'output_kind':enum('facets','evaluation'),'result':{'anyOf':[ref('PluginArtifact'),{'type':'null'}]}})
    schemas['PluginBatch']=obj({**BATCH['properties'],'contribution_id':ID,'output_kind':enum('facets','evaluation'),'jobs':{'type':'array','items':ref('PluginJob'),'maxItems':50}})
    schemas['PluginInput']={'type':'object','description':'固定轨迹快照、贡献点定义、output_schema 和绑定输入的 input_ref。'}
    replacements={'PluginVersion':'ExtensionVersion','PluginManifest':'ExtensionManifest','EvaluationBatch':'PluginBatch','EvaluationJob':'PluginJob','CreateEvaluation':'CreatePluginRun','EvaluationScore':'PluginOutput','EvaluationResult':'PluginArtifact','EvaluationInput':'PluginInput'}
    def rewrite(value):
        if isinstance(value,list):return [rewrite(v) for v in value]
        if isinstance(value,dict):return {k:rewrite(v) for k,v in value.items()}
        if isinstance(value,str) and value.startswith('#/components/schemas/'):
            name=value.split('/')[-1];return '#/components/schemas/'+replacements.get(name,name)
        return value
    for path,operations in list(paths.items()):
        if path in ('/api/plugins','/api/plugin-schema','/api/evaluation-score-schema') or path.startswith(('/api/evaluations','/api/evaluation-jobs')):
            renamed=path.replace('/api/evaluation-jobs','/api/plugin-jobs').replace('/api/evaluations','/api/plugin-runs')
            renamed={'/api/plugins':'/api/extensions','/api/plugin-schema':'/api/extension-schema','/api/evaluation-score-schema':'/api/plugin-output-schema'}.get(renamed,renamed)
            operations=rewrite(copy.deepcopy(operations))
            for method,operation in operations.items():
                operation['operationId']='extension_'+operation['operationId'];operation['tags']=['Extensions']
                if renamed=='/api/extensions' and method=='get':operation['summary']='统一列出原生能力与兼容评分器；不执行插件'
                elif renamed=='/api/extensions':operation['summary']='注册通用插件描述；浏览器代码须随前端发布'
            paths[renamed]=operations
