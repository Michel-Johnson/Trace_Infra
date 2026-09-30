import { useEffect, useMemo, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import { Link } from 'react-router-dom';
import { infraApi, type ImportBatch, type InfraTask, type TaskKind, type TaskState, type TaskStep } from '../api/infra-client';
import { groupInfraTasks, taskBatchId, type InfraTaskGroup } from '../lib/task-groups';
import { linkTasksToAgentSessions, loadTaskAgentSessions, taskAgentHref, type TaskAgentTarget } from '../lib/task-agent-link';
import { WorkspaceSelect, type WorkspaceSelectOption } from './WorkspaceSelect';
import './Workbench.css';

type StageState = 'done' | 'active' | 'blocked' | 'pending';
type Stage = { label: string; state: StageState; detail?: string };
type WorkbenchTask = {
  id: string; title: string; kind: TaskKind; state: TaskState; updatedAt: string;
  stages: Stage[]; issue?: string; impact?: string; result?: string; taskId?: string;
  agentSession?: TaskAgentTarget | null;
  agentUnlinked?: boolean;
};

const demoMode = import.meta.env.MODE === 'mock';
const kindLabels: Record<TaskKind, string> = { adapter_import: '导入', evaluation: '评测', analysis: '分析', custom: '任务' };
const stageLabels: Record<string, string> = {
  format_inspect: '识别源格式', adapter_select: '编写 Adapter', adapter_validate: '样本校验',
  trace_import: '批量导入', readback: '建立检索索引',
  execute: 'Agent 执行', capture: 'Trace 归档',
};
const demoProject: WorkspaceSelectOption<string> = { value: 'benchmark-a', label: 'Agent Trace Benchmark A' };
const demoTasks: WorkbenchTask[] = [
  { id: 'demo-adapter', kind: 'adapter_import', title: 'Claude Code 会话导入', state: 'failed', updatedAt: '2026-09-28T09:30:00Z', issue: 'Adapter 缺少来源字段', impact: '80 条 Trace 暂未导入', stages: [
    { label: '识别源格式', state: 'done' }, { label: '编写 Adapter', state: 'blocked' },
    { label: '样本校验', state: 'pending' }, { label: '批量导入与索引', state: 'pending' },
  ] },
  { id: 'demo-import', kind: 'adapter_import', title: 'Benchmark A 轨迹导入', state: 'running', updatedAt: '2026-09-28T09:20:00Z', stages: [
    { label: '准备 Adapter', state: 'done' }, { label: '样本校验', state: 'done' },
    { label: '批量导入', state: 'active', detail: '已导入 600 / 1,000' }, { label: '建立检索索引', state: 'pending' },
  ] },
  { id: 'demo-evaluation', kind: 'evaluation', title: '工具调用准确性评测', state: 'running', updatedAt: '2026-09-28T09:10:00Z', stages: [
    { label: '确定数据与规则', state: 'done' }, { label: '逐条评测', state: 'active', detail: '已评测 18 / 50' },
    { label: '汇总结果', state: 'pending' }, { label: '复核异常', state: 'pending' },
  ] },
  { id: 'demo-analysis-running', kind: 'analysis', title: '高耗时 Span 分析', state: 'running', updatedAt: '2026-09-28T09:05:00Z', stages: [
    { label: '确认分析范围', state: 'done' }, { label: '聚合耗时', state: 'active', detail: '已分析 24 / 120' },
    { label: '定位异常', state: 'pending' }, { label: '复核证据', state: 'pending' },
  ] },
  { id: 'demo-doubao-import', kind: 'adapter_import', title: '豆包会话轨迹导入', state: 'running', updatedAt: '2026-09-28T09:00:00Z', stages: [
    { label: '识别源格式', state: 'done' }, { label: '编写 Adapter', state: 'active', detail: '已映射 12 / 18 个字段' },
    { label: '样本校验', state: 'pending' }, { label: '导入 Trace', state: 'pending' }, { label: '回读结果', state: 'pending' },
  ] },
  { id: 'demo-analysis-done', kind: 'analysis', title: 'Trace Deep Dive', state: 'succeeded', updatedAt: '2026-09-28T08:20:00Z', stages: [], result: '发现 6 处异常调用' },
  { id: 'demo-evaluation-done', kind: 'evaluation', title: '工具调用评测', state: 'succeeded', updatedAt: '2026-09-28T08:10:00Z', stages: [], result: '42 通过 · 8 待复核' },
];

function errorText(task: InfraTask): string {
  if (typeof task.error === 'string') return task.error;
  if (task.error && typeof task.error === 'object') return task.error.message || task.error.code || '';
  return '';
}

function normalizeStageState(value: string | undefined): StageState {
  const state = (value || '').toLowerCase();
  if (['succeeded', 'success', 'completed', 'complete', 'done'].includes(state)) return 'done';
  if (['failed', 'error', 'blocked'].includes(state)) return 'blocked';
  if (['running', 'active', 'in_progress', 'processing'].includes(state)) return 'active';
  return 'pending';
}

function stageName(step: TaskStep): string {
  return step.label || stageLabels[step.id || ''] || (step.id || '任务处理').replaceAll('_', ' ');
}

function taskStages(task: InfraTask, state: TaskState, detail?: string): Stage[] {
  const stages: Stage[] = task.steps.map(step => ({ label: stageName(step), state: normalizeStageState(step.state) }));
  if (!stages.length) {
    const label = task.current_stage ? stageLabels[task.current_stage] || task.current_stage : state === 'queued' ? '等待执行' : '任务处理';
    return [{ label, state: state === 'failed' ? 'blocked' : state === 'succeeded' ? 'done' : 'active', detail }];
  }
  if (state === 'failed' && !stages.some(stage => stage.state === 'blocked')) {
    const index = stages.findIndex(stage => stage.state !== 'done');
    stages[index < 0 ? stages.length - 1 : index].state = 'blocked';
  }
  if (state === 'running' && !stages.some(stage => stage.state === 'active')) {
    const index = stages.findIndex(stage => stage.state === 'pending');
    if (index >= 0) stages[index].state = 'active';
  }
  const current = stages.find(stage => stage.state === 'active');
  if (current && detail) current.detail = detail;
  return stages;
}

function taskResult(task: InfraTask): string | undefined {
  if (!task.result || typeof task.result !== 'object') return undefined;
  const result = task.result as Record<string, unknown>;
  return typeof result.summary === 'string' && result.summary.trim() ? result.summary.trim() : undefined;
}

function taskCard(group: InfraTaskGroup, tasks: InfraTask[], batch?: ImportBatch): WorkbenchTask {
  const members = tasks.filter(task => group.taskIds.includes(task.task_id));
  const task = members.find(item => item.state === 'failed') || members.find(item => item.state === 'running') || members[0];
  const state = members.some(item => item.state === 'failed') ? 'failed' : group.state;
  const count = batch ? { processed: batch.completed, total: batch.total }
    : group.isBatch ? { processed: group.completedCount, total: null }
      : { processed: task.processed, total: task.total };
  const detail = state === 'running' && count.total !== null && count.total > 0
    ? `已处理 ${count.processed.toLocaleString()} / ${count.total.toLocaleString()}` : undefined;
  const issue = members.map(errorText).find(Boolean) || (state === 'failed'
    ? `${stageLabels[group.currentStage] || group.currentStage || '当前阶段'}受阻` : undefined);
  const remaining = count.total === null ? null : Math.max(0, count.total - count.processed);
  const impact = state === 'failed' && remaining !== null && remaining > 0 ? `${remaining.toLocaleString()} 项待处理` : undefined;
  return {
    id: group.id, kind: group.kind, state, updatedAt: group.updatedAt,
    title: group.isBatch ? 'Trace 批量导入' : task.title || task.task_id,
    stages: taskStages(task, state, detail), issue, impact, result: taskResult(task),
    taskId: task.task_id,
    agentSession: group.agentSession,
    agentUnlinked: group.agentUnlinked,
  };
}

function StageList({ stages, horizontal = false }: { stages: Stage[]; horizontal?: boolean }) {
  const stageColumns = horizontal ? { '--thw-stage-count': stages.length } as CSSProperties : undefined;
  return <ol className={`thw-stages${horizontal ? ' thw-stages-horizontal' : ''}`} style={stageColumns} aria-label="任务阶段">
    {stages.map((stage, index) => <li className={`thw-stage thw-stage-${stage.state}`} aria-current={stage.state === 'active' || stage.state === 'blocked' ? 'step' : undefined} key={`${stage.label}-${index}`}>
      <strong>{stage.label}</strong><span className="thw-sr-only">{stage.state === 'done' ? '已完成' : stage.state === 'active' ? '进行中' : stage.state === 'blocked' ? '在此受阻' : '待开始'}</span>
      {stage.detail && <span className="thw-stage-detail">{stage.detail}</span>}
    </li>)}
  </ol>;
}

function KindTag({ kind }: { kind: TaskKind }) {
  return <span className={`thw-kind thw-kind-${kind}`}>{kindLabels[kind]}</span>;
}

export function Workspace() {
  const [topbarTarget, setTopbarTarget] = useState<HTMLElement | null>(null);
  const [project, setProject] = useState(() => demoMode ? demoProject.value : localStorage.getItem('trace-hunter-infra-project') || '');
  const [projects, setProjects] = useState<Array<WorkspaceSelectOption<string>>>(demoMode ? [demoProject] : []);
  const [tasks, setTasks] = useState<InfraTask[]>([]);
  const [batches, setBatches] = useState<Map<string, ImportBatch>>(new Map());
  const [loading, setLoading] = useState(!demoMode);
  const [error, setError] = useState('');

  useEffect(() => { setTopbarTarget(document.getElementById('thw-topbar-controls')); }, []);

  useEffect(() => {
    if (demoMode) return;
    const controller = new AbortController();
    let active = true;
    void infraApi.projects(controller.signal).then(value => {
      if (!active) return;
      const options = value.items.map(item => ({ value: item.project_id, label: item.name }));
      setProjects(options);
      if (!options.some(item => item.value === project)) setProject(options[0]?.value || '');
      if (!options.length) setLoading(false);
    }).catch(reason => { if (active) { setError((reason as Error).message); setLoading(false); } });
    return () => { active = false; controller.abort(); };
  }, []);

  useEffect(() => {
    if (demoMode || !project) return;
    const controller = new AbortController();
    let inFlight = false;
    const refresh = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const value = await infraApi.tasks(project, { limit: 200 }, controller.signal);
        const linkedTasks = value.items.some(task => task.source && ['agent', 'auto_import'].includes(
          String((task.source as Record<string, unknown>).type)))
          ? linkTasksToAgentSessions(value.items, await loadTaskAgentSessions()) : value.items;
        const batchIds = Array.from(new Set(value.items.map(taskBatchId).filter(Boolean)));
        const summaries = await Promise.all(batchIds.map(async id => {
          try { return [id, await infraApi.importBatch(project, id, { limit: 1 }, controller.signal)] as const; }
          catch { return null; }
        }));
        if (!controller.signal.aborted) {
          setTasks(linkedTasks);
          setBatches(new Map(summaries.filter((item): item is readonly [string, ImportBatch] => Boolean(item))));
          setError('');
        }
      } catch (reason) {
        if (!controller.signal.aborted) setError((reason as Error).message);
      } finally {
        inFlight = false;
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    setTasks([]);
    setBatches(new Map());
    setError('');
    setLoading(true);
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15_000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [project]);

  const cards = useMemo(() => demoMode ? demoTasks : groupInfraTasks(tasks).map(group => taskCard(group, tasks, batches.get(group.title))), [tasks, batches]);
  const ordered = [...cards].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  const attention = ordered.filter(task => task.state === 'failed').slice(0, 1);
  const running = ordered.filter(task => task.state === 'running' || task.state === 'queued').slice(0, demoMode ? 4 : 2);
  const completed = ordered.filter(task => task.state === 'succeeded').slice(0, 2);
  const selectProject = (value: string) => { setProject(value); if (!demoMode) localStorage.setItem('trace-hunter-infra-project', value); };

  return <>{topbarTarget && createPortal(<div className="thw-topbar-controls" aria-label="工作台操作">
    {!demoMode && projects.length > 0 && <WorkspaceSelect value={project} options={projects} onChange={selectProject} ariaLabel="工作台项目" />}
    {demoMode && <span className="thw-demo-label">演示数据</span>}
    <Link to="/imports">导入 Trace</Link><Link to="/infra">运行分析</Link><Link to="/plugins/evaluations/designer">设计评测</Link>
  </div>, topbarTarget)}<div className="thw-workbench" aria-label="工作台任务总览">
    {error && <div className="thw-error" role="alert">任务读取失败：{error}</div>}
    {loading && !cards.length && <div className="thw-loading">正在读取任务…</div>}
    {attention.length > 0 && <section className="thw-section" aria-labelledby="thw-attention-title"><div className="thw-section-head"><h2 id="thw-attention-title">需要处理</h2></div>
      {attention.map(task => <article className="thw-blocked" key={task.id}>
        <div className="thw-blocked-head"><h3 className="thw-blocked-name">{task.title}</h3>
          <div className="thw-blocked-issue"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v6"/><path d="M12 17h.01"/></svg><span>{task.issue || '任务未完成'}</span></div>
          <div className="thw-card-actions">{task.agentSession && <Link to={taskAgentHref('/', '', task.agentSession)}>打开 Agent 会话</Link>}
            {task.agentUnlinked && <span className="thw-session-unavailable">无可打开的会话</span>}
            <Link to={task.taskId ? `/tasks?project=${encodeURIComponent(project)}&task=${encodeURIComponent(task.taskId)}` : '/tasks'}>查看原因</Link></div>
        </div><StageList stages={task.stages} horizontal />
      </article>)}
    </section>}
    {running.length > 0 && <section className="thw-section" aria-labelledby="thw-running-title"><div className="thw-section-head"><h2 id="thw-running-title">正在运行</h2></div>
      <div className="thw-running-grid">{running.map(task => <article className="thw-running-card" key={task.id}>
        <div className="thw-running-head"><div className="thw-running-identity"><KindTag kind={task.kind}/><h3>{task.title}</h3></div>
          <div className="thw-card-actions">{task.agentSession && <Link className="thw-action-button" to={taskAgentHref('/', '', task.agentSession)}>打开 Agent 会话</Link>}
            {task.agentUnlinked && <span className="thw-session-unavailable">无可打开的会话</span>}
            <Link className="thw-action-button" to={task.taskId ? `/tasks?project=${encodeURIComponent(project)}&task=${encodeURIComponent(task.taskId)}` : '/tasks'}>查看任务</Link></div></div>
        <StageList stages={task.stages} horizontal />
      </article>)}</div>
    </section>}
    {completed.length > 0 && <section className="thw-section" aria-labelledby="thw-completed-title"><div className="thw-section-head"><h2 id="thw-completed-title">最近完成</h2><Link className="thw-action-button" to="/tasks">查看全部任务</Link></div>
      <div className="thw-completed-list">{completed.map(task => <article className="thw-completed-row" key={task.id}>
        <div className="thw-completed-identity"><KindTag kind={task.kind}/><h3>{task.title}</h3></div>{task.result && <strong>{task.result}</strong>}
        <div className="thw-card-actions">{task.agentSession && <Link className="thw-action-button" to={taskAgentHref('/', '', task.agentSession)}>打开 Agent 会话</Link>}
          {task.agentUnlinked && <span className="thw-session-unavailable">无可打开的会话</span>}
          <Link className="thw-action-button" to={task.taskId ? `/tasks?project=${encodeURIComponent(project)}&task=${encodeURIComponent(task.taskId)}` : '/tasks'}>{task.kind === 'analysis' ? '查看证据' : '查看结果'}</Link></div>
      </article>)}</div>
    </section>}
    {!loading && !error && !cards.length && <section className="thw-empty"><h2>还没有任务</h2><Link to="/imports">导入第一批 Trace</Link></section>}
  </div></>;
}
