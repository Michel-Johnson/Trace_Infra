import type { InfraProject, TraceItem } from '../api/infra-client';

// This is a UI-only collection. Real project IDs remain on each trace for readback.
export const agentTraceGroupId = '__agent-traces__';

export type ProjectTraceItem = TraceItem & { project_id: string; display_title?: string };

export function withAgentTraceLabels(
  rows: ProjectTraceItem[],
  labels: Array<{ project_id: string; run_id: string; title: string }>,
): ProjectTraceItem[] {
  const titles = new Map(labels.map(item => [`${item.project_id}\u0000${item.run_id}`, item.title]));
  return rows.map(row => ({ ...row, display_title: titles.get(`${row.project_id}\u0000${row.run_id}`) || row.display_title }));
}

export function isAgentTraceProject(project: InfraProject): boolean {
  return /^agent-[0-9a-f]{20}$/.test(project.project_id) && project.name.startsWith('Agent Trace · ');
}

export function agentTraceProjects(projects: InfraProject[]): InfraProject[] {
  return projects.filter(isAgentTraceProject);
}

export function traceProjectOptions(projects: InfraProject[]): Array<{ value: string; label: string }> {
  const ordinary = projects.filter(project => !isAgentTraceProject(project))
    .map(project => ({ value: project.project_id, label: project.name }));
  if (agentTraceProjects(projects).length) ordinary.push({ value: agentTraceGroupId, label: 'Agent Trace' });
  return ordinary;
}

export function traceProjectSelection(projects: InfraProject[], projectId: string): string {
  return projects.some(project => project.project_id === projectId && isAgentTraceProject(project))
    ? agentTraceGroupId : projectId;
}

export function mergeProjectTraces(groups: Array<{ project_id: string; items: TraceItem[] }>): ProjectTraceItem[] {
  return groups.flatMap(group => group.items.map(item => ({ ...item, project_id: group.project_id })))
    .sort((left, right) => {
      const byDate = String(right.created_at || '').localeCompare(String(left.created_at || ''));
      return byDate || left.project_id.localeCompare(right.project_id) || String(left.run_id || '').localeCompare(String(right.run_id || ''));
    });
}
