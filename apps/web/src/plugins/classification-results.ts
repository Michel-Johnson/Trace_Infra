/** Host-owned projection of immutable classification artifacts across execution batches. */
import type {Bundle} from '../api/types';
import type {PluginBatch,PluginJob} from '../api/extensions';
import type {Classification} from './classification-view';

const canonical=(value:unknown):string=>JSON.stringify(value,(_key,child)=>
  child&&typeof child==='object'&&!Array.isArray(child)?Object.fromEntries(Object.entries(child).sort(([a],[b])=>a.localeCompare(b))):child);

type ContextGroup={batches:PluginBatch[];success:Map<string,PluginJob>;latest:Map<string,PluginJob>};
export type LatestClassification=PluginBatch & {aggregate:true;batch_ids:string[];latest_jobs:PluginJob[]};
export const classificationContextId=(batch:Pick<PluginBatch,'plugin_id'|'plugin_version'|'contribution_id'|'scope'|'config'>)=>
  'latest:'+canonical([batch.plugin_id,batch.plugin_version,batch.contribution_id,batch.scope,batch.config]);

const bundleMap=(bundles:Bundle[])=>new Map(bundles.map(bundle=>[bundle.run.id,bundle]));
function validResult(job:PluginJob,batch:PluginBatch,bundles:Map<string,Bundle>,scope:string):boolean {
  const bundle=bundles.get(job.run_id),result=job.result;
  return !!bundle&&job.input_digest===bundle.digest&&job.scope===scope&&batch.scope===scope
    &&job.state==='completed'&&result?.output.kind==='facets'
    &&result.input_ref.document_id===job.run_id&&result.input_ref.document_digest===bundle.digest
    &&result.input_ref.selection_digest===job.snapshot_digest&&result.scope===scope
    &&result.plugin_ref.id===batch.plugin_id&&result.plugin_ref.version===batch.plugin_version
    &&result.contribution_id===batch.contribution_id&&canonical(job.config)===canonical(batch.config);
}

export function validClassificationJobs(batch:PluginBatch|undefined,bundles:Bundle[],scope:string):PluginJob[]{
  if(!batch)return [];
  const selected=bundleMap(bundles);
  return batch.jobs.filter(job=>validResult(job,batch,selected,scope));
}

/** Latest successful result per run, with progress from its latest matching request.
 * New queued/failed attempts do not hide an older valid success. Scope, version and
 * canonical configuration each define a separate context. Historical batches stay untouched.
 */
export function latestClassifications(batches:PluginBatch[],bundles:Bundle[],scope:string):LatestClassification[]{
  const selected=bundleMap(bundles),groups=new Map<string,ContextGroup>();
  const ordered=[...batches].sort((a,b)=>b.created_at.localeCompare(a.created_at)||b.id.localeCompare(a.id));
  for(const batch of ordered){
    if(batch.output_kind!=='facets'||batch.scope!==scope)continue;
    const jobs=batch.jobs.filter(job=>selected.get(job.run_id)?.digest===job.input_digest&&job.scope===scope);
    if(!jobs.length)continue;
    const id=classificationContextId(batch);
    const group:ContextGroup=groups.get(id)||{batches:[],success:new Map(),latest:new Map()};
    group.batches.push(batch);groups.set(id,group);
    for(const job of jobs){
      if(!group.latest.has(job.run_id))group.latest.set(job.run_id,job);
      if(!group.success.has(job.run_id)&&validResult(job,batch,selected,scope))group.success.set(job.run_id,job);
    }
  }
  return [...groups].map(([id,group])=>{
    const base=group.batches[0],latest_jobs=bundles.flatMap(b=>group.latest.has(b.run.id)?[group.latest.get(b.run.id)!]:[]);
    const counts=Object.fromEntries(['queued','running','completed','failed','cancelled'].map(state=>[state,latest_jobs.filter(j=>j.state===state).length])) as PluginBatch['counts'];
    return {...base,id,aggregate:true,batch_ids:group.batches.map(b=>b.id),latest_jobs,counts,
      jobs:bundles.flatMap(b=>group.success.has(b.run.id)?[group.success.get(b.run.id)!]:[])};
  });
}

/** No artifact => absent run key. Artifact with no assignment => an empty Map. */
export function classificationRows(jobs:PluginJob[],facetId:string|undefined):Classification['rows']{
  const rows:Classification['rows']={};
  for(const job of jobs){
    const output=job.result?.output;
    if(output?.kind!=='facets')continue;
    rows[job.run_id]=new Map(output.data.assignments.filter(a=>a.facet_id===facetId).map(a=>[a.target.entity_id,{status:a.status,values:a.value_ids}]));
  }
  return rows;
}

export function defaultClassification(contexts:LatestClassification[],classifier:string,config:unknown):LatestClassification|undefined {
  return contexts.find(context=>`${context.plugin_id}@${context.plugin_version}:${context.contribution_id}`===classifier
    &&canonical(context.config)===canonical(config));
}
