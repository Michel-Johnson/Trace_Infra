export type RepositorySkill = {
  id:string;
  title:string;
  description:string;
  href:string;
  source:'infra'|'repository';
};

const documents=import.meta.glob('../../../../skills/*/SKILL.md',{query:'?raw',import:'default',eager:true}) as Record<string,string>;
const infraRoutes:Record<string,string>={
  'trace-eval-designer':'/plugins/evaluations/designer',
  'trace-deep-dive':'/plugins/evaluations/deep-dive',
  'trace-batch-analyzer':'/plugins/evaluations/batch-analyzer',
  'trace-analysis-reporter':'/plugins/reports',
  'trace-hunter-adapter':'/plugins/adapter',
};

function frontmatter(source:string,key:string){
  const match=source.match(new RegExp(`^${key}:\\s*(.+)$`,'m'));
  return match?.[1]?.trim().replace(/^("|')|("|')$/g,'')||'';
}

export const repositorySkills:RepositorySkill[]=Object.entries(documents).map(([path,source])=>{
  const fallback=path.split('/').at(-2)||'skill';
  const id=frontmatter(source,'name')||fallback;
  const title=source.match(/^#\s+(.+)$/m)?.[1]?.trim()||id;
  const description=frontmatter(source,'description')||'仓库中保存的 Agent Skill。';
  const href=infraRoutes[id]||`/plugins/skills/${encodeURIComponent(id)}`;
  return {id,title,description,href,source:(infraRoutes[id]?'infra':'repository') as RepositorySkill['source']};
}).sort((left,right)=>left.title.localeCompare(right.title,'zh-CN'));

export function repositorySkill(id:string){
  return repositorySkills.find(item=>item.id===id);
}
