import { useEffect, useState } from 'react';
import { App as AntApp } from 'antd';
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { copyText } from '../lib/clipboard';
import { analysisSkillConfigs, type AnalysisSkillKind } from '../skills/trace-analysis';
import { repositorySkill, repositorySkills } from '../skills/catalog';
import { buildSkillInstallPrompt } from '../skills/install';
import { WorkspaceIcon as Icon } from './WorkspaceIcon';
import { Workspace } from './Workspace';
import { AgentTerminal } from './AgentTerminal';
import type { TaskAgentTarget } from '../lib/task-agent-link';
import { InfraHealthPage, InfraImportPage, InfraQueryPage, InfraSearchPage, InfraTasksPage, InfraTracePage } from './InfraPages';
import './Plugins.css';
import './SkillPages.css';

type Props = { path: string[] };
const nav = [
  { id: 'overview', group: '工作区', label: '工作台', icon: 'home' as const, href: '/' },
  { id: 'traces', group: '可观测性', label: '轨迹', icon: 'file' as const, href: '/traces' },
  { id: 'search', group: 'Trace Infra', label: 'Span 搜索', icon: 'search' as const, href: '/search' },
  { id: 'infra', group: 'Trace Infra', label: '查询与分析', icon: 'pulse' as const, href: '/infra' },
  { id: 'imports', group: 'Trace Infra', label: '导入', icon: 'upload' as const, href: '/imports' },
  { id: 'tasks', group: 'Trace Infra', label: '任务进度', icon: 'check' as const, href: '/tasks' },
  { id: 'health', group: 'Trace Infra', label: '存储与质量', icon: 'capture' as const, href: '/health' },
  { id: 'plugins', group: '扩展', label: '插件与 Skill', icon: 'check' as const, href: '/plugins' },
];
export function AppShell(props: Props) {
  const { message } = AntApp.useApp();
  const location = useLocation();
  const navigate = useNavigate();
  const [agentOpen, setAgentOpen] = useState(false);
  const sessionParams = new URLSearchParams(location.search);
  const agentSessionId = sessionParams.get('agent_session');
  const requestedKind = sessionParams.get('agent_session_kind');
  const agentSessionKind: TaskAgentTarget['kind'] = requestedKind === 'interactive' ? 'interactive' : 'native_import';
  const resumableSessionId = requestedKind === 'interactive' || requestedKind === 'native_import'
    ? agentSessionId : null;
  const [agentMounted, setAgentMounted] = useState(!!resumableSessionId);
  useEffect(() => { if (resumableSessionId) setAgentMounted(true); }, [resumableSessionId]);
  function selectAgentSession(id: string | null, kind?: TaskAgentTarget['kind']) {
    const params = new URLSearchParams(location.search);
    if (id) {
      params.set('agent_session', id);
      params.set('agent_session_kind', kind || 'native_import');
    } else {
      params.delete('agent_session');
      params.delete('agent_session_kind');
    }
    navigate({ pathname: location.pathname, search: params.toString() }, { replace: true });
  }
  function closeAgent() {
    setAgentOpen(false);
    if (agentSessionId) selectAgentSession(null);
  }
  const section = props.path[0] || 'overview';
  const active = section;
  const detailId = props.path[1] ? decodeURIComponent(props.path[1]) : '';
  const pageTitle = section === 'overview' ? '工作台'
    : section === 'traces' ? '轨迹库'
    : section === 'search' ? 'Span 搜索'
    : section === 'infra' ? '查询与分析'
    : section === 'imports' ? 'Trace 导入'
    : section === 'tasks' ? '任务进度'
    : section === 'health' ? '存储与数据质量'
    : section === 'plugins' && detailId === 'evaluations' && props.path[2] ? analysisSkillConfigs[props.path[2] as AnalysisSkillKind]?.name || '评测'
    : section === 'plugins' && detailId === 'evaluations' ? '评测'
    : section === 'plugins' && detailId === 'reports' ? '分析报告'
    : section === 'plugins' && detailId === 'adapter' ? '格式接入'
    : section === 'plugins' && detailId === 'skills' && props.path[2] ? repositorySkill(props.path[2])?.title || 'Skill'
    : section === 'plugins' ? '插件与 Skill'
    : section === 'capture' ? '采集接入'
    : 'Trace Hunter';
  async function copySkillInstallPrompt() {
    try {
      await copyText(buildSkillInstallPrompt());
      message.success('安装 Prompt 已复制，请发送给你的 Agent');
    } catch (reason) {
      message.warning(reason instanceof Error ? reason.message : '复制失败，请重试');
    }
  }
  const pageAction = section === 'capture'
        ? <Link className="th-topbar-primary" to="/imports"><Icon name="upload" size={14}/>导入 JSON</Link>
        : section === 'plugins'
          ? <button className="th-topbar-primary" type="button" onClick={() => void copySkillInstallPrompt()}><Icon name="copy" size={15}/>安装 Skill</button>
        : null;
  return <div className="th-shell">
    <aside className="th-sidebar">
      <Link className="th-brand" to="/"><span className="brand-mark" aria-hidden="true"><i/><i/><i/><i/><i/><i/><i/><i/><i/></span><span><strong>Trace Hunter</strong></span></Link>
      <nav className="th-navigation" aria-label="工作区导航">{nav.map(item => <div className="th-nav-group" key={item.id}><Link className={active === item.id ? 'active' : ''} to={item.href}><Icon name={item.icon} size={16} /><span>{item.label}</span></Link></div>)}</nav>
      <div className="th-sidebar-footer"><a href="/about.html"><Icon name="book" size={15} />产品说明</a><span className="th-app-version">v0.5.4</span></div>
    </aside>
    <div className="th-main">
      <header className={section === 'overview' ? 'th-topbar th-topbar-workbench' : 'th-topbar'}><h1 className="th-topbar-page-title">{pageTitle}</h1><div className="th-top-actions">{section === 'overview' && <div id="thw-topbar-controls" />}{pageAction}<button className="th-assistant" aria-label="打开 Trace Hunter Agent" title="Trace Hunter Agent" onClick={() => { setAgentMounted(true); setAgentOpen(true); }}><img src="/claude-code-agent.svg" alt=""/></button><button className="th-avatar">TH</button></div></header>
      <main className={'th-content th-section-' + active}>
        <AppRouter {...props} section={section} />
      </main>
    </div>
    {agentMounted ? <AgentTerminal onClose={closeAgent} visible={agentOpen || !!resumableSessionId}
      initialSessionId={resumableSessionId} initialSessionKind={agentSessionKind}
      onSessionChange={selectAgentSession}/> : null}
  </div>;
}

function AppRouter(props: Props & { section: string }) {
  if (props.section === 'collections' || props.section === 'cases') return <Navigate to="/traces" replace />;
  if (props.section === 'traces') return <InfraTracePage runId={props.path[1] ? decodeURIComponent(props.path[1]) : undefined} />;
  if (props.section === 'search') return <InfraSearchPage />;
  if (props.section === 'infra') return <InfraQueryPage />;
  if (props.section === 'imports') return <InfraImportPage />;
  if (props.section === 'tasks') return <InfraTasksPage />;
  if (props.section === 'health') return <InfraHealthPage />;
  if (props.section === 'plugins') return <Plugins view={props.path[1]} skill={props.path[2]} />;
  if (props.section === 'capture') return <Capture />;
  return <Workspace />;
}

function Plugins({ view,skill }: { view?: string;skill?: string }) {
  if (view === 'evaluations' && skill === 'designer') return <EvalDesigner />;
  if (view === 'evaluations' && (skill === 'deep-dive' || skill === 'batch-analyzer')) return <AnalysisSkill kind={skill} />;
  if (view === 'evaluations') return <Navigate to="/plugins" replace />;
  if (view === 'reports') return <AnalysisSkill kind="analysis-reporter" />;
  if (view === 'adapter') return <AdapterSkill />;
  if (view === 'skills' && skill) return <RepositorySkill id={skill} />;
  const items = repositorySkills.map(item=>({key:item.id,title:item.title,detail:skillCardDescriptions[item.id]||item.description,version:item.source==='repository'?'仓库 Skill':'',href:item.href,kind:'skill' as const,icon:(item.id==='trace-hunter-adapter'?'capture':'spark') as 'capture'|'spark',art:skillArtSlot(item.id)}));
  return <div className="th-plugin-manager"><section className="th-plugin-manager-list" aria-label="Trace Infra Skills">{items.map(item => <Link className="th-plugin-manager-row" to={item.href} key={item.key}>
    <span className={`th-plugin-manager-icon ${item.kind}`} data-skill-art={item.art}><Icon name={item.icon} size={22}/></span>
    {item.kind==='skill'?<span className="th-plugin-manager-status">开发中</span>:null}
    <span className="th-plugin-manager-copy" data-skill={item.key}><span><strong>{item.title}</strong></span><span>{item.detail}</span></span>
  </Link>)}</section></div>;
}

const skillCardDescriptions:Record<string,string>={
  'trace-hunter':'按任务选择 Skill\n规划分析与执行顺序',
  'trace-hunter-cli':'通过平台 CLI 导入、检索和分析 Trace',
  'doubao-trace-import':'整理豆包工作轨迹与完整会话，校验来源、字段结构和时间关系并标准化导入',
  'claude-trace-import':'导入 Claude Code 会话与工具调用轨迹，校验调用链、时间关系和采集覆盖',
  'trace-analysis-reporter':'汇总评测证据、分析结果与关键案例，生成可追溯、可复核的结构化审计报告',
  'trace-batch-analyzer':'依据已批准的评测规范批量分析轨迹，统计命中、异常、边界与未知结果',
  'trace-deep-dive':'精读完整轨迹、关键上下文与工具调用，定位行为异常、失败原因和恢复路径',
  'trace-eval-designer':'澄清评测目标、数据范围与判定口径，设计可执行、可复核的评测规范',
  'trace-hunter-adapter':'识别外部轨迹格式、来源与字段结构，转换并校验为 Trace Hunter 标准数据',
};
const skillArtSlots:Record<string,string>={
  'doubao-trace-import':'0','claude-trace-import':'1','trace-analysis-reporter':'2','trace-batch-analyzer':'3',
  'trace-deep-dive':'4','trace-eval-designer':'5','trace-hunter-adapter':'6','collect-session-trace':'7',
  'trace-hunter':'trace-hunter',
};
function skillArtSlot(skill:string){return skillArtSlots[skill]||'7';}

type SkillExample = {title:string;prompt:string};
type SkillDetailProps = {name:string;skill:string;description:string;examples:SkillExample[];parent?:string;status?:string};

function SkillDetailPage({name,skill,description,examples,parent='/plugins',status='开发中'}:SkillDetailProps) {
  const {message}=AntApp.useApp();
  const [copied,setCopied]=useState<number|null>(null);
  async function copy(value:string,index:number){try{await copyText(value);setCopied(index);message.success('示例指令已复制');}catch(reason){message.warning(reason instanceof Error?reason.message:'复制失败，请手动复制');}}
  return <div className="th-page th-skill-detail">
    <div className="th-backline"><Link to={parent}>插件与 Skill</Link><Icon name="chevron" size={14}/><span>{name}</span></div>
    <section className="th-skill-profile">
      <span className="th-skill-profile-icon" data-skill-art={skillArtSlot(skill)} aria-hidden="true"/>
      <div><h1>{name}</h1><p>{description}</p></div>
      <b><i/>{status}</b>
    </section>
    <section className="th-skill-usage">
      <header><h2>如何使用</h2></header>
      <div className="th-skill-example-list">{examples.map((example,index)=><article key={example.title}>
        <div><h3>{example.title}</h3><span><strong>${skill}</strong>{example.prompt.replace(`$${skill}`,'')}</span></div>
        <button type="button" onClick={()=>void copy(example.prompt,index)} aria-label={`复制${example.title}指令`}><Icon name={copied===index?'check':'copy'} size={18}/>{copied===index?'已复制':'复制'}</button>
      </article>)}</div>
    </section>
  </div>;
}

const analysisSkillExamples:Record<AnalysisSkillKind,SkillExample[]>={
  'deep-dive':[
    {title:'校准评测规则',prompt:'$trace-deep-dive 按 Draft Eval Spec 精读项目 agent-regression 中的成功、失败和边界样本，输出逐条 DeepDiveResult JSONL 与规则反馈。'},
    {title:'检查 Skill 遵循情况',prompt:'$trace-deep-dive 按给定 Eval Spec 检查这些 Trace 是否遵循目标 Skill；只使用当时可见的上下文，输出带证据的 deep-dive-results.jsonl。'},
  ],
  'batch-analyzer':[
    {title:'执行已批准评测',prompt:'$trace-batch-analyzer 执行 /path/eval-spec.json，对其中固定的 Trace revisions 做全量分析并输出版本化结果。'},
    {title:'全量检查工具调用',prompt:'$trace-batch-analyzer 按 Approved Eval Spec 检查工具调用合理性，完整统计命中、未命中、边界和 unknown。'},
  ],
  'analysis-reporter':[
    {title:'生成单次分析报告',prompt:'$trace-analysis-reporter 根据 /path/analysis-result.json 生成一份可审计的 HTML 报告，突出覆盖率、关键案例和限制。'},
    {title:'汇总完整评测材料',prompt:'$trace-analysis-reporter 把 Eval Spec、Analysis Result 和 DeepDiveResult JSONL 汇总成面向研发评审的 Markdown 报告。'},
  ],
};

function AnalysisSkill({kind}:{kind:AnalysisSkillKind}) {
  const config=analysisSkillConfigs[kind];
  return <SkillDetailPage name={config.name} skill={config.skill} description={config.description} examples={analysisSkillExamples[kind]}/>;
}

function EvalDesigner() {
  return <SkillDetailPage name="Trace Eval Designer" skill="trace-eval-designer" description="把模糊的评测想法整理成可评审、可执行的 Eval Spec，并明确证据、范围、判定和 unknown 语义。" examples={[
    {title:'评测 Skill 性能成本',prompt:'$trace-eval-designer 帮我设计一项评测：检查目标 Skill 是否造成异常 Token 消耗和长尾延迟。'},
    {title:'评测工具调用合理性',prompt:'$trace-eval-designer 设计一项工具调用合理性评测；先澄清会影响结论的问题，本轮只输出 Draft Eval Spec。'},
  ]}/>;
}

function AdapterSkill() {
  return <SkillDetailPage name="Trace Hunter Adapter" skill="trace-hunter-adapter" description="识别外部 Agent Trace 的来源格式，复用或修复 Adapter，并转换为可校验的 Trace Hunter v2 数据。" examples={[
    {title:'转换并导入 Trace',prompt:'$trace-hunter-adapter 把 /path/run.json 转成 Trace Hunter v2，校验通过后导入项目 agent-regression。'},
    {title:'检查或修复 Adapter',prompt:'$trace-hunter-adapter 检查这批 JSON 是否已有可用 Adapter；如果格式变化，修复并输出转换报告，不要导入。'},
  ]}/>;
}

function RepositorySkill({id}:{id:string}) {
  const skill=repositorySkill(id);
  if (!skill) return <div className="th-error">Skill 不存在或尚未同步到当前仓库</div>;
  return <SkillDetailPage name={skill.title} skill={skill.id} description={skill.description} examples={[
    {title:'调用此 Skill',prompt:`$${skill.id} 请根据我的目标执行任务；开始前先确认输入、输出和必要边界。`},
    {title:'先确认能力范围',prompt:`$${skill.id} 请先说明你能处理的任务、需要的输入和不会执行的操作，再等我提供材料。`},
  ]}/>;
}

function Capture() {
  return <SkillDetailPage name="会话 Trace 采集" skill="collect-session-trace" status="未开发" description="该通用采集 Skill 尚未开发。当前页面仅展示规划能力，不能直接采集本机 Agent 会话；现阶段请导出已有轨迹文件后通过导入页处理。" examples={[
    {title:'规划用法：采集当前会话',prompt:'$collect-session-trace 采集当前 Agent 会话，导出原始文件和 Trace Hunter 标准 run.trace.json。'},
    {title:'规划用法：选择历史会话',prompt:'$collect-session-trace 列出本机可采集的会话，等我选定后再导出，并保留完整多轮记录和来源信息。'},
  ]}/>;
}

function Loading() { return <div className="th-loading"><i/><i/><i/><i/></div>; }
