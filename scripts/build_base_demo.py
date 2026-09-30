"""Build clearly synthetic Base 4+1 demo inputs; never invokes Base or a model."""
import hashlib
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from trace_hunter.protocol import validate
from trace_hunter.catalog import validate_catalog

QUERY='【合成演示】搭建超市进货系统：商品与进货表、进货登记表单、低库存提醒工作流、采购仪表盘、店长和采购员权限。'
COLLECTION='base-4plus1-demo-v1'
CASE='demo-base-building-4plus1'

def build(route):
    trace=json.loads((ROOT/'examples/minimal.trace.json').read_text())
    trace['run'].update(id=f'demo-base-4plus1-{route}-v1',query_id=CASE,env_id='demo-base-synthetic-v1',
        title=f'合成演示 · {route.title()} 到 4+1 Domain',query=QUERY,
        harness=f'Base {route.title()} 路线（演示）',model='合成数据 · 非模型实测',
        time_basis='合成演示时钟；所有时长和 token 为预设示意值，不能用于模型或产品性能比较。')
    trace['collector']={'name':'synthetic-base-demo','version':'1.0.0'}
    trace['environment']={'isolation':'unknown','network_access':'unknown','observed_at':None,'snapshot_id':None,
        'tool_versions':{},'notes':'纯合成演示，未实际调用模型、Base 或外部服务；不是沙箱实跑结果。'}
    trace['sources']=[{'id':'original','name':'合成脚本 scripts/build_base_demo.py；非真实采集',
        'sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}]
    trace['spans']=[];trace['links']=[];trace['evidence']=[]
    clock=0;sequence=0
    def step(agent,name,operation,params,model_s,tool_s,output=None):
        nonlocal clock,sequence
        sequence+=1;mid=f'model-{sequence:02d}';tid=f'tool-{sequence:02d}'
        def span(sid,kind,label,op,start,end,data,result):
            return {'id':sid,'kind':kind,'name':label,'operation':op,'agent_id':agent,'parent_id':None,
                'request_id':f'request-{sequence:02d}' if kind=='model' else None,'attempt':1,'phase_id':'task',
                'start_ms':start,'end_ms':end,'duration_ms':None,'status':'ok','input':data,'output':result,
                'usage':None,'source':{'source_id':'original','pointer':f'/synthetic/{sid}'}}
        model=span(mid,'model','Model','other',clock,clock+model_s*1000,None,{'demo':True})
        model['usage']={'input_tokens':1000+sequence*100,'output_tokens':200+sequence*10,
            'cache_read_tokens':500,'cache_write_tokens':0,'thinking_tokens':100}
        clock+=model_s*1000
        tool=span(tid,'tool',name,operation,clock if tool_s is not None else None,
            clock+tool_s*1000 if tool_s is not None else None,params,output or {'demo':True,'recorded_result':'示意成功'})
        tool.update(sequence=sequence,order_basis='source_sequence')
        if tool_s is None:tool['status']='unknown'
        trace['spans'].extend([model,tool]);trace['links'].append({'from':mid,'to':tid,'type':'invokes'})
        clock+=(tool_s or 0)*1000
    def wait(agent,seconds):
        nonlocal clock
        trace['spans'].append({'id':'confirm-wait','kind':'wait','name':'用户确认（演示）','operation':'human',
            'agent_id':agent,'parent_id':None,'request_id':None,'attempt':1,'phase_id':'task',
            'start_ms':clock,'end_ms':clock+seconds*1000,'duration_ms':None,'status':'ok',
            'input':{'question':'确认此合成方案？'},'output':{'reply':'确认（模拟）'},'usage':None,
            'source':{'source_id':'original','pointer':'/synthetic/confirm-wait'}})
        clock+=seconds*1000
    step('main','Read','read',{'path':'/demo/skills/lark-base/SKILL.md'},1,.2)
    if route=='plan':
        step('host_create_plan_agent','create_plan','write',{'targets':['table','form','workflow','dashboard','permission']},10,.4)
        wait('host_plan_agent',12)
    else:
        step('requirements_generator','Write','write',{'path':'/demo/spec.json','content':'五域的需求设计（合成）'},9,.3)
        step('workflow_designer','Write','write',{'path':'/demo/spec-workflow.json','content':'只设计低库存提醒，还未执行工作流。'},7,.2)
        wait('spec_pro',18)
    step('table','create_plan','write',{'scope':'table','description':'Table 内部规划，不是顶层 Plan'},4,.2)
    step('main','Bash','bash',{'command':'lark-cli base +base-create --name demo-store --table-name 商品 --fields @demo-fields.json'},8,2)
    step('main','Bash','bash',{'command':'lark-cli base +table-create --base-token demo-base --name 进货 --fields @demo-purchases.json'},7,3)
    step('main','Bash','bash',{'command':'lark-cli base +record-batch-create --base-token demo-base --table-id demo-table --json @demo-records.json'},3,1)
    step('form','Bash','bash',{'command':'lark-cli base +form-create --base-token demo-base --table-id demo-table'},5,1)
    step('workflow_adapter_agent','create_plan','write',{'scope':'workflow','description':'Flow 内部规划，归属 Flow'},4,.3)
    step('main','Bash','bash',{'command':'lark-cli base +workflow-create --base-token demo-base --json @demo-flow.json'},8,2)
    step('main','Bash','bash',{'command':'lark-cli base +workflow-enable --base-token demo-base --workflow-id demo-flow'},1,.5)
    if route=='spec':
        step('main','external_operation','other',{'note':'缺少用途与执行计时的合成调用，保留未知'},2,None)
    step('main','Bash','bash',{'command':'lark-cli base +dashboard-create --base-token demo-base --name 采购概览'},6,1)
    step('perm','create_role','write',{'roles':['店长','采购员'],'note':'合成权限配置'},5,1.5)
    step('host_critic_agent','Read','read',{'path':'/demo/checks.json'},4,.4)
    step('host_critic_summarize_agent','present_files','write',{'files':['/demo/使用说明.md'],'demo':True},2,.2)
    trace['phases']=[{'id':'task','name':'Base 生成全流程（合成）','purpose':'task','start_ms':0,'end_ms':clock}]
    validate(trace)
    return trace

def catalog():
    value={'schema_version':'trace-hunter/catalog/1.0','collection':{'id':COLLECTION,'title':'Base 4+1 Domain 演示（合成）','kind':'task',
        'description':'交互演示数据：四域 Table / Dashboard / Workflow / Permission，加 Form；所有耗时与 token 都是示意值。'},
        'cases':[{'query_id':CASE,'title':'Base 生成：Plan / Spec → 4+1 Domain','description':'两条合成路线演示，不代表生产流程必须按此顺序串行执行。',
                  'conversation':{'mode':'single_turn','turns':[{'id':'query-1','prompt':QUERY}]}}]}
    validate_catalog(value)
    return value

if __name__=='__main__':
    directory=ROOT/'examples/base-building-demo';directory.mkdir(exist_ok=True)
    for name,value in [('catalog.json',catalog()),('plan.trace.json',build('plan')),('spec.trace.json',build('spec'))]:
        (directory/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
        print(name)
