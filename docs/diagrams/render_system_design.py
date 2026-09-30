"""Render the system design figures with the Python standard library.

Author-owned, static SVGs: no remote scripts, fonts, or diagram dependencies.
Run: python3 docs/diagrams/render_system_design.py
"""
from pathlib import Path
from html import escape
OUT=Path(__file__).resolve().parent
class Diagram:
    def __init__(self,title,description,height=630,width=1000):
        self.w=width;self.h=height
        self.parts=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc"><title id="title">{escape(title)}</title><desc id="desc">{escape(description)}</desc>',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#69757b"/></marker></defs>',
        '<style>text{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;fill:#232b30;font-size:14px}.small{font-size:12px;fill:#677279}.title{font-weight:600;font-size:16px}.label{font-size:12px;fill:#4f616c}.note{font-size:12px;fill:#52646d}.future{stroke-dasharray:6 4}</style>',f'<rect width="{width}" height="{height}" fill="white"/>']
    def text(self,x,y,label,cls='',anchor='start'):
        self.parts.append(f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}">{escape(label)}</text>')
    def box(self,x,y,w,h,title,lines=(),future=False,fill='#f8fafb'):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="3" fill="{fill}" stroke="#bac5cb" class="{"future" if future else ""}"/>')
        self.text(x+w/2,y+27,title,'title','middle')
        for i,line in enumerate(lines):self.text(x+w/2,y+49+i*20,line,'small','middle')
    def line(self,points,label=None,at=None,dashed=False,arrow=True):
        path=' '.join(('M' if i==0 else 'L')+f'{x},{y}' for i,(x,y) in enumerate(points))
        attributes = (' stroke-dasharray="5 4"' if dashed else '') + (' marker-end="url(#arrow)"' if arrow else '')
        self.parts.append(f'<path d="{path}" fill="none" stroke="#74818a" stroke-width="1.3"{attributes}/>')
        if label:self.text(*at,label,'label')
    def note(self,x,y,w,text):
        self.parts.append(f'<rect x="{x}" y="{y-18}" width="{w}" height="30" fill="#eef3f5"/>');self.text(x+10,y+2,text,'note')
    def save(self,name):
        (OUT/(name+'.svg')).write_text('\n'.join(self.parts+['</svg>'])+'\n')

def current():
    d=Diagram('当前部署架构','Web、采集器和远程 Agent 经 Caddy 进入 FastAPI。API 与两个官方 Worker 复用 Python 核心。PostgreSQL 保存登记、索引、任务和产物，持久内容目录保存正文。远程 Agent 通过 API，不直接连接数据库。',710)
    d.box(40,25,270,72,'Web / UI 插件',['React · 浏览与显式分析'])
    d.box(365,25,270,72,'采集器 / CLI / Skill',['标准 JSON · 保留来源'])
    d.box(690,25,270,72,'远程 Agent / 服务',['查询凭据 · 任务凭据'])
    for x in (175,500,825):d.line([(x,97),(x,128),(500,128)],arrow=False)
    d.line([(500,128),(500,157)])
    d.box(180,157,640,72,'同源入口 · Caddy',['网页登录 / 项目与任务 Bearer 转发；权限由 API 再校验'])
    d.line([(500,229),(500,263)])
    d.box(150,263,700,160,'API 进程 · FastAPI',['路由、鉴权、限额、错误映射'],fill='#f7f9fa')
    d.box(180,338,640,61,'Backend · 共享 Python 应用服务',['版本 / 查询 / 选集 / 产物 / Invocation / 授权'],fill='#fff')
    d.line([(275,423),(275,464)],'独立进程，共享核心',(75,449),arrow=False)
    d.line([(535,423),(535,464)],'事务与查询',(548,450))
    d.line([(765,423),(765,464)],'正文读写',(778,450))
    d.box(40,464,320,112,'两个官方 Worker',['旧分类 / 评测 Worker','中性 Invocation Worker','领取任务 → 执行 → 提交结果'])
    d.box(420,464,230,112,'PostgreSQL',['版本、元数据与索引','固定选集、租约、产物来源','同库持久任务'])
    d.box(705,464,255,112,'持久内容目录',['LocalContentStore','原始 JSON / 产物正文','按 SHA-256 校验'])
    d.line([(360,510),(420,510)])
    d.line([(200,576),(200,608),(835,608),(835,576)])
    d.text(455,598,'Worker 读写内容存储','label','middle')
    d.note(40,652,920,'当前单机 systemd 部署；容器角色已经拆分，Compose 仍需补齐 Invocation Worker。')
    d.save('system-current')

def target():
    d=Diagram('目标能力架构','以已有版本、查询和执行核心连接业务插件，逐步建设回放与验证、数据发布和训练消费。虚线模块尚待建设；对象存储、分析读库与分片由实测规模触发。',665)
    d.box(35,25,280,72,'人 / Agent',['浏览器 · API · CLI / MCP'])
    d.box(360,25,605,72,'协议与服务身份',['能力发现 · 精确版本引用 · 项目与任务授权'])
    d.line([(315,61),(360,61)])
    d.line([(660,97),(660,138)])
    d.box(35,138,930,110,'中性平台核心',['记录与修订 · 内容存储 · 有界查询 · 固定选集','Invocation / Attempt · Typed Artifact · 证据与来源'],fill='#edf3f6')
    d.line([(160,248),(160,295)]);d.line([(500,248),(500,295)]);d.line([(835,248),(835,295)],dashed=True)
    d.box(35,295,255,95,'业务插件',['Base 阶段 / 成本 / 质量','分类、评分、统计、渲染'])
    d.box(360,295,280,95,'远程 Runtime',['服务握手与任务委派','业务代码、模型、Agent'])
    d.box(710,295,255,95,'回放与验证',['初态恢复 / 重执行','新轨迹 / 独立验收'],future=True)
    d.line([(965,222),(983,222),(983,477),(730,477)],dashed=True)
    d.text(745,464,'固定输入与发布引用','label')
    d.box(270,435,460,84,'数据发布与训练消费',['固定样本与转换版本 → 分片 → 训练回执'],future=True)
    d.box(35,556,930,75,'按负载扩展的数据实现',['S3 对象提供方 / 存储分块 / 分析读库：达到实测门槛后引入'],future=True)
    d.line([(35,220),(17,220),(17,593),(35,593)],dashed=True)
    d.text(35,649,'实线：已有核心或接入能力；虚线：待建设能力。图中方框是职责，不要求一框一个微服务。','small')
    d.save('system-target')

def model():
    d=Diagram('资源关系与轨迹内部结构','集合与 Case 组织运行，运行可有多个不可变版本。每个版本记录轮次、模型请求、工具调用和关联。固定选集绑定版本，Invocation 使用固定输入并生成独立业务产物。',650)
    d.box(30,28,270,66,'Collection / Case',['组织关系 · 不改变轨迹原文'])
    d.box(365,28,270,66,'Run',['一次执行；重跑产生新 Run'])
    d.box(700,28,270,66,'Trace Revision',['补采 / 修复产生新版本'])
    d.line([(300,61),(365,61)]);d.line([(635,61),(700,61)])
    d.line([(835,94),(835,134),(500,134),(500,170)])
    d.box(170,170,660,242,'版本内的记录与关系',[],fill='#fafbfc')
    d.text(203,225,'用户轮次 01  /  Query + 本轮响应','title')
    d.text(238,264,'模型请求 A  →  响应 / 工具调用提议','')
    d.text(275,302,'工具执行 1 / 2  →  结果观察','')
    d.text(238,340,'模型请求 B  →  最终回复','')
    d.text(203,382,'用户轮次 02  /  追加要求 …','title')
    d.line([(218,237),(218,337),(229,337)],arrow=False)
    d.line([(255,275),(255,298),(266,298)],arrow=False)
    d.line([(830,264),(914,264),(914,375),(830,375)],'来源 / 因果关联',(843,331),dashed=True)
    d.line([(500,412),(500,439),(165,439),(165,463)])
    d.text(220,431,'固定成员引用','label')
    d.line([(605,412),(605,463)],'也可直接引用',(616,451))
    d.box(30,463,270,78,'Selection Snapshot',['固定 Run + Revision + Digest'])
    d.box(365,463,270,78,'Invocation / Attempt',['操作版本 · 配置 · 本次执行'])
    d.box(700,463,270,78,'Artifact',['分类 / 分数 / 报告 / 验收'])
    d.line([(300,502),(365,502)]);d.line([(635,502),(700,502)])
    d.line([(834,541),(834,581),(166,581),(166,541)],'产物保留固定输入和生成者来源',(348,571),dashed=True)
    d.note(30,621,940,'树用于阅读；并行、重试、共享结果和子 Agent 依赖使用显式关联，不能只靠时间顺序猜。')
    d.save('system-resources')

def sequence(name,title,desc,actors,rows,notes=()):
    height=145+len(rows)*60+len(notes)*38
    d=Diagram(title,desc,height)
    xs=[80+i*(840/(len(actors)-1)) for i in range(len(actors))]
    for x,actor in zip(xs,actors):
        d.box(x-73,20,146,63,actor[0],actor[1:])
        d.line([(x,83),(x,height-35)],dashed=True,arrow=False)
    for i,(src,dst,label,kind) in enumerate(rows):
        y=124+i*60
        if src==dst:
            x=xs[src];d.line([(x,y),(x+37,y),(x+37,y+21),(x,y+21)],dashed=kind=='return')
            d.text(x+45,y+6,label,'label')
        else:
            a,b=xs[src],xs[dst]
            d.line([(a,y+14),(b,y+14)],dashed=kind=='return')
            d.text((a+b)/2,y,label,'label','middle')
    for i,note in enumerate(notes):d.note(25,135+len(rows)*60+i*38,950,note)
    d.save(name)

current();target();model()
sequence('system-ingest','时序：导入、登记与索引','当前版本入口先保存校验后的内容，再通过数据库事务发布版本登记；随后建立索引。索引失败保留已登记原件，幂等重传不自动重建索引。',[('采集器 / Agent','提交原始 JSON'),('API / 核心','校验与版本登记'),('内容存储','不可变字节'),('PostgreSQL','元数据与索引')],[
(0,1,'1. Idempotency-Key + expected_previous + 原文','call'),
(1,2,'2. 写入正文，计算 SHA-256','call'),
(2,1,'3. 返回内容引用','return'),
(1,3,'4. 事务：幂等检查、版本并发校验、登记 revision','call'),
(3,1,'5. 版本登记提交','return'),
(1,3,'6. 建立该 projector 的记录索引与状态','call'),
(3,1,'7. complete / failed / 本次索引失败信息','return'),
(1,0,'8. 已登记版本 + 索引状态','return'),
(0,1,'9. 修复后显式请求 index 重建','call')],[
'索引失败不会撤销已保存的原件；重复导入只读取已有状态，避免浏览或重传暗中启动计算。',
'这是当前同步导入路径。分阶段上传和异步索引是后续扩展，不能画成已部署的大规模流管道。'])
sequence('system-analyze','时序：查询、固定输入与远程插件','Agent 先查询固定选集，再由调度方创建任务、固定操作和运行时并委派执行凭据。远端读取输入、续租、提交结果，平台在事务内发布结果、产物和血缘。',[('人 / 查询 Agent','读取或显式计算'),('API / 核心','权限与执行合同'),('PostgreSQL','选集 / 租约 / 产物'),('调度方 / 远程 Agent','授权后执行插件')],[
(0,1,'1. 查询 / 聚合 / 读取已有分析','call'),
(1,0,'2. 返回分页数据、覆盖情况和引用','return'),
(0,1,'3. 显式创建 Selection + Invocation','call'),
(1,2,'4. 固定输入版本、操作与配置','call'),
(3,1,'5. 授权调度者领取；按需发放任务凭据','call'),
(1,3,'6. attempt / 租约 / 绑定输入','return'),
(3,1,'7. 读取固定输入；执行期间 heartbeat','call'),
(3,1,'8. complete：输出角色与内容','call'),
(1,2,'9. 正文先存；SQL 事务登记结果、产物引用与来源','call'),
(1,3,'10. 固定回执；相同结果重传返回原回执','return'),
(0,1,'11. 读取 Artifact；按需创建下一次 Invocation','call')],[
'远程 Runtime 先登记和探测，再委派；官方 Worker 直接调用进程内核心，复用相同执行生命周期。',
'服务身份与任务凭据权限不同。租约只控制提交资格，不证明外部动作已经停止或只执行一次。'])
sequence('system-replay','目标时序：回放、独立验收与训练发布','此图为待建设目标。回放固定输入和环境，恢复初态后创建新执行轨迹，再由独立验收生成报告。仅在通过版本化准入政策后发布训练数据，训练侧提交有范围的消费回执。',[('发起方','固定目标与预算'),('平台核心','版本 / 权限 / 来源'),('回放 Runtime','恢复环境 / 新执行'),('Verifier / 训练端','验收 / 消费')],[
(0,1,'1. 固定 Trace + 环境快照 + 回放模式','call'),
(1,2,'2. 授权执行，校验初态与工具版本','call'),
(2,1,'3. 新 Run + 产物 + 环境证据','return'),
(1,3,'4. 独立验证：固定原要求、产物和验收器版本','call'),
(3,1,'5. 通过 / 失败 / 缺证据，附证据引用','return'),
(0,1,'6. 显式申请训练发布，执行准入政策','call'),
(1,3,'7. 固定 Release + 转换版本 + 分片摘要','call'),
(3,1,'8. 按任务 / attempt / epoch / step 报告消费','return'),
(1,0,'9. 回查：模型 checkpoint → 消费 → 样本 → 原轨迹','return')],[
'回放、独立验收、Dataset Release 和训练消费账本仍待建设；当前 API 没有承诺这些成品资源。',
'环境恢复失败与业务失败分开；下载不计为训练，回执表示训练端报告，不证明样本对权重的因果贡献。'])
