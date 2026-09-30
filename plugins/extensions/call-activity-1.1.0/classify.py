"""Classify recorded tool operations without assigning quality scores."""
import json
import sys

def evaluate(context):
    values=[('read','读取'),('write','写入'),('execute','执行'),('interaction','交互')]
    assignments=[]
    for row in context['input']['tool_rows']:
        value={'read':'read','write':'write','bash':'execute','human':'interaction'}.get(row['operation'])
        target={'document_id':context['input']['run']['id'],'document_digest':context['input_digest'],'entity_kind':'span','entity_id':row['span_id']}
        assignments.append({'target':target,'facet_id':'activity','status':'assigned' if value else 'unknown','value_ids':[value] if value else [],
            'reason':'按已记录的 operation 字段分类。' if value else '原始记录未明确归入读取、写入、执行或交互，保留未知。','evidence_refs':[target]})
    return {'kind':'facets','data':{'schema_version':'trace-hunter/facets/1.0','input_refs':[context['input_ref']], 'coverage':'complete',
        'facets':[{'id':'activity','title':'调用活动','cardinality':'single','values':[{'id':key,'title':label} for key,label in values]}], 'assignments':assignments},
        'usage':{'input_tokens':0,'output_tokens':0,'cost':0,'currency':None}}

if __name__=='__main__':print(json.dumps(evaluate(json.load(sys.stdin)),ensure_ascii=False,allow_nan=False))
