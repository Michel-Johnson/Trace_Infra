import type {Bundle,Row} from '../api/types';
import type {FacetAssignment} from './slicers';

export type Classification = {
  value:string; rows:Record<string,Map<string,FacetAssignment>>;
  dimension?:{id:string;title:string;values:{id:string;title:string}[]};
  batchId?:string;
};
export type CallOrder = 'source'|'classification'|'tool_ms'|'response_ms'|'cycle_ms';
export type IndexedRow = {row:Row;index:number};
export const classificationKey = (a?:FacetAssignment) => !a||a.status==='unknown'?'unknown':a.status==='not_applicable'?'not_applicable':a.values.length===1?'value:'+a.values[0]:'multiple';
export function classificationGroups(classification:Classification){
  return [...(classification.dimension?.values||[]).map(v=>({id:'value:'+v.id,title:v.title})),
    {id:'multiple',title:'多分类'}, {id:'not_applicable',title:'不适用'}, {id:'unknown',title:'未知 / 未分类'}];
}
export function orderCalls(rows:IndexedRow[],order:CallOrder,assignments?:Map<string,FacetAssignment>,groups:{id:string}[]=[]){
  const sorted=[...rows];
  if(order==='source')return sorted.sort((a,b)=>a.index-b.index);
  if(order==='classification')return sorted.sort((a,b)=>{
    const position=(row:Row)=>{const found=groups.findIndex(g=>g.id===classificationKey(assignments?.get(row.span_id)));return found<0?groups.length:found;};
    return position(a.row)-position(b.row)||a.index-b.index;
  });
  return sorted.sort((a,b)=>{
    const av=a.row[order],bv=b.row[order];
    return av==null?(bv==null?a.index-b.index:1):bv==null?-1:bv-av||a.index-b.index;
  });
}

type Span=Bundle['trace']['spans'][number];
type Total={count:number;known:number;ms:number};
const tokenFields=['input_tokens','output_tokens','cache_read_tokens','cache_write_tokens','thinking_tokens'] as const;
type TokenField=typeof tokenFields[number];
type TokenCount={value:number;known:number};
export type StageTiming={id:string;title:string;tools:Total;models:Total;waits:Total;
  timeShare:number|null;timeCoverage:number|null;
  tokens:Record<TokenField|'total_tokens',TokenCount>;tokenConflicts:number;
  turns:{ids:string[];ordinals:number[];known:number;count:number;partial:boolean}};
const total=():Total=>({count:0,known:0,ms:0});
const duration=(s:Span)=>s.start_ms!=null&&s.end_ms!=null?s.end_ms-s.start_ms:s.duration_ms;
const add=(target:Total,ms:number|null)=>{target.count++;if(ms!=null){target.known++;target.ms+=ms;}};
const stage=(g:{id:string;title:string}):StageTiming=>({...g,tools:total(),models:total(),waits:total(),
  timeShare:null,timeCoverage:null,
  tokens:Object.fromEntries([...tokenFields,'total_tokens'].map(k=>[k,{value:0,known:0}])) as StageTiming['tokens'],tokenConflicts:0,
  turns:{ids:[],ordinals:[],known:0,count:0,partial:false}});
export function tokenLabel(value:TokenCount,requests:number){
  return value.known?`${value.known<requests?'已记录 ':''}${value.value.toLocaleString('en-US')}`:'未知';
}
export function turnLabel(turns:StageTiming['turns']){
  if(!turns.ids.length)return '轮次未知';
  const partial=turns.partial||turns.known<turns.count;
  return `${partial?'已关联 ':''}${turns.ids.length} 轮用户对话（第 ${turns.ordinals.join('、')} 轮）`;
}
export function stageTimeLabel(s:StageTiming){
  const values=[s.tools,s.models,s.waits],known=values.reduce((n,v)=>n+v.known,0),count=values.reduce((n,v)=>n+v.count,0);
  if(!known)return '耗时 未知';
  const ms=values.reduce((n,v)=>n+v.ms,0);
  return `耗时 ${(ms/1000).toLocaleString('en-US',{maximumFractionDigits:2})} 秒${known<count?'*':''}`;
}

// Duration is workload, not wall time. Model snapshots of the same request are deduplicated.
export function stageTiming(bundle:Bundle,classification:Classification):StageTiming[]{
  const assignments=classification.rows[bundle.run.id],groups=classificationGroups(classification);
  const result=new Map(groups.map(g=>[g.id,stage(g)]));
  result.set('shared',stage({id:'shared',title:'跨分类共享请求'}));
  const bucket=(id:string)=>result.get(result.has(id)?id:'unknown')!;
  const spans=bundle.trace.spans.filter(s=>bundle.view.scope.includes(s.phase_id));
  const requests=new Map<string,Span[]>();
  const addTurns=(target:StageTiming,parts:Span[])=>{
    target.turns.count++;
    const turns=parts.map(s=>bundle.view.span_turns?.[s.id]).filter(t=>t!=null);
    const ids=new Set(turns.map(t=>t.turn_id));
    if(ids.size!==1)return;
    const turn=turns[0];target.turns.known++;
    target.turns.partial ||= turns.some(t=>t.coverage!=='complete');
    if(!target.turns.ids.includes(turn.turn_id)){
      target.turns.ids.push(turn.turn_id);target.turns.ordinals.push(turn.ordinal);
      target.turns.ordinals.sort((a,b)=>a-b);
    }
  };
  for(const span of spans){
    const id=classificationKey(assignments?.get(span.id));
    if(span.kind==='tool'){add(span.operation==='human'?bucket(id).waits:bucket(id).tools,duration(span));addTurns(bucket(id),[span]);}
    else if(span.kind==='wait'){add(bucket(id).waits,duration(span));addTurns(bucket(id),[span]);}
    else if(span.kind==='model'){
      const key=JSON.stringify([span.agent_id,span.request_id||span.id,span.attempt]);
      requests.set(key,[...(requests.get(key)||[]),span]);
    }
  }
  for(const parts of requests.values()){
    const ids=new Set(parts.map(s=>classificationKey(assignments?.get(s.id))));
    const id=ids.size===1?[...ids][0]:'shared';
    // Only identical timing snapshots are safe to merge. Conflicting fragments stay unknown.
    const values=new Set(parts.map(s=>JSON.stringify([s.start_ms,s.end_ms,s.duration_ms])));
    const group=bucket(id);
    add(group.models,values.size===1?duration(parts[0]):null);addTurns(group,parts);
    const populated=parts.map(s=>s.usage).filter(u=>u!=null);
    const usages=new Set(populated.map(u=>JSON.stringify(tokenFields.map(k=>u[k]))));
    if(usages.size>1){group.tokenConflicts++;continue;}
    const usage=populated[0];if(!usage)continue;
    for(const field of tokenFields){const value=usage[field];if(value!=null){group.tokens[field].known++;group.tokens[field].value+=value;}}
    // Cache is already included in input; thinking is already included in output.
    // Only complete input/output pairs contribute to the total, never disjoint partial requests.
    if(usage.input_tokens!=null&&usage.output_tokens!=null){group.tokens.total_tokens.known++;group.tokens.total_tokens.value+=usage.input_tokens+usage.output_tokens;}
  }
  const stages=[...result.values()].filter(g=>g.tools.count+g.models.count+g.waits.count>0);
  // Share uses the recorded workload across every category, including unknown and shared.
  // Concurrent work is additive here; dividing by elapsed wall time would mislead.
  const recordedMs=stages.reduce((sum,g)=>sum+g.tools.ms+g.models.ms+g.waits.ms,0);
  for(const group of stages){
    const values=[group.tools,group.models,group.waits];
    const known=values.reduce((n,v)=>n+v.known,0),count=values.reduce((n,v)=>n+v.count,0);
    group.timeCoverage=count?known/count:null;
    group.timeShare=known&&recordedMs>0?values.reduce((n,v)=>n+v.ms,0)/recordedMs:null;
  }
  return stages;
}
