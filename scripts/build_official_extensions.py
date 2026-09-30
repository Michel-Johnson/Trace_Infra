"""Build the first-party extension manifest from explicit shipped files."""
import hashlib
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from trace_hunter.catalog import canonical

def build():
    files={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['plugins/extensions/call-activity/classify.py','apps/web/src/plugins/renderers.tsx','apps/web/src/plugins/slicers.ts']}
    def contribution(cid,title,kind,ref,host,trigger,output,permissions,**extra):
        return {'id':cid,'title':title,'kind':kind,'implementation':{'host':host,'ref':ref},'scopes':['run'],
            'consumes':['trace-hunter/1.0','trace-hunter/1.1'],'config_schema':{'type':'object','properties':{},'required':[],'additionalProperties':False},
            'default_config':{},'trigger':trigger,'output_kind':output,'permissions':permissions,**extra}
    contributes=[
        contribution('grid','调用方格','renderer','trace.grid','browser','view','view',['trace.read'],mounts=['run.timeline']),
        contribution('list','调用列表','renderer','trace.list','browser','view','view',['trace.read'],mounts=['run.timeline']),
        contribution('operations','工具类型切片','slicer','trace.operations','browser','interaction','selection',['trace.read'],mode='filter'),
        contribution('facets','分类切片','slicer','trace.facets','browser','interaction','selection',['trace.read','derived.read'],mode='filter'),
        contribution('classify','调用活动分类','slicer','classify.py','server','explicit','facets',['trace.read','facets.submit'],mode='classify'),
    ]
    contributes[3]['consumes'].append('trace-hunter/facets/1.0')
    manifest={'schema_version':'trace-hunter/plugin/2.0','plugin_id':'official.call-activity','version':'1.0.0','title':'调用视图与活动分类',
        'description':'方格与列表共用切片条件；按已记录操作生成调用分类，保留未知，不产生评分。','package_digest':canonical(files)[1],'contributes':contributes,'extensions':{}}
    return manifest,{'files':files}

def main():
    manifest,package=build();directory=ROOT/'plugins/extensions/call-activity'
    for filename,value in [('manifest.json',manifest),('package.json',package)]:
        (directory/filename).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__':main()
