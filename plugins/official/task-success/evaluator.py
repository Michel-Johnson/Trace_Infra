"""Verify an explicit acceptance checklist against recorded assertions."""
import json
import sys


def evaluate(context):
    required=context['config']['required_checks']; records={e['id']:e for e in context['input']['evidence'] if e['kind']=='assertion'}
    missing=[key for key in required if key not in records or records[key]['status'] not in ('passed','failed')]
    failed=[key for key in required if key in records and records[key]['status']=='failed']
    ready=bool(required) and not missing
    reason='全部指定验收项通过。' if ready and not failed else '存在未通过的指定验收项。' if ready else '尚未指定完整验收项清单。' if not required else '缺少可判定的验收证据：'+', '.join(missing)
    refs=[{'kind':'evidence','id':key} for key in required if key in records]
    findings=[{'code':'acceptance_failed','severity':'error','message':records[key]['name']+'：验收未通过。','repair_suggestion':None,'evidence':[{'kind':'evidence','id':key}]} for key in failed]
    if not ready:findings.append({'code':'acceptance_missing','severity':'warning','message':reason,'repair_suggestion':'补充所需验收项的断言记录，再重新评测。','evidence':[]})
    state='evaluated' if ready else 'insufficient_data'
    return {'status':state,'metrics':[{'key':'task_passed','value':not failed if ready else None,'status':state,'reason':reason,'evidence':refs}], 'findings':findings,'usage':{'input_tokens':0,'output_tokens':0,'cost':0,'currency':None}}

if __name__=='__main__':print(json.dumps(evaluate(json.load(sys.stdin)),ensure_ascii=False,allow_nan=False))
