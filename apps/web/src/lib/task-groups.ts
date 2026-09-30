import type { InfraTask, TaskArtifact, TaskKind, TaskState } from '../api/infra-client';
import { taskAgentTarget, type TaskAgentTarget } from './task-agent-link';

export type InfraTaskGroup = {
  id: string;
  kind: TaskKind;
  title: string;
  state: TaskState;
  currentStage: string;
  progress: number;
  completedCount: number;
  itemCount: number;
  updatedAt: string;
  taskIds: string[];
  activeTaskIds: string[];
  artifacts: TaskArtifact[];
  errors: string[];
  isBatch: boolean;
  agentSession: TaskAgentTarget | null;
  agentUnlinked: boolean;
};

export function taskBatchId(task: InfraTask) {
  if (!task.source || typeof task.source !== 'object') return '';
  const batchId = (task.source as Record<string, unknown>).batch_id;
  return typeof batchId === 'string' ? batchId.trim() : '';
}

function groupState(tasks: InfraTask[]): TaskState {
  if (tasks.some(task => task.state === 'running')) return 'running';
  if (tasks.some(task => task.state === 'queued')) return 'queued';
  if (tasks.some(task => task.state === 'failed')) return 'failed';
  if (tasks.every(task => task.state === 'cancelled')) return 'cancelled';
  return 'succeeded';
}

function errorText(task: InfraTask) {
  if (typeof task.error === 'string') return task.error;
  return task.error?.message || task.error?.code || (task.error ? JSON.stringify(task.error) : '');
}

export function groupInfraTasks(tasks: InfraTask[]): InfraTaskGroup[] {
  const buckets = new Map<string, InfraTask[]>();
  for (const task of tasks) {
    const batchId = task.kind === 'adapter_import' ? taskBatchId(task) : '';
    const key = batchId ? `batch:${batchId}` : `task:${task.task_id}`;
    const bucket = buckets.get(key);
    if (bucket) bucket.push(task);
    else buckets.set(key, [task]);
  }
  return Array.from(buckets.entries()).map(([key, items]) => {
    const first = items[0];
    const batchId = key.startsWith('batch:') ? key.slice(6) : '';
    const state = groupState(items);
    const progress = items.reduce((sum, task) => sum + Math.max(0, Math.min(1, Number.isFinite(task.progress) ? task.progress : 0)), 0) / items.length;
    const active = items.filter(task => task.state === 'queued' || task.state === 'running');
    const current = active.find(task => task.state === 'running') || active[0]
      || items.find(task => task.state === 'failed');
    const agentSession = batchId ? null : taskAgentTarget(first);
    return {
      id: key,
      kind: first.kind,
      title: batchId || first.title || first.task_id,
      state,
      currentStage: current?.current_stage || (state === 'queued' ? '等待执行' : ''),
      progress,
      completedCount: items.filter(task => task.state === 'succeeded').length,
      itemCount: items.length,
      updatedAt: items.reduce((latest, task) => task.updated_at > latest ? task.updated_at : latest, first.updated_at),
      taskIds: items.map(task => task.task_id),
      activeTaskIds: active.map(task => task.task_id),
      artifacts: items.flatMap(task => task.artifacts),
      errors: items.map(errorText).filter(Boolean),
      isBatch: Boolean(batchId),
      agentSession,
      agentUnlinked: !batchId && !agentSession && Boolean(first.source &&
        typeof first.source === 'object' && ['agent', 'auto_import'].includes(
          String((first.source as Record<string, unknown>).type))),
    };
  });
}
