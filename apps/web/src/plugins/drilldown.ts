import type {FacetAssignment} from './slicers';

export const drilldownDimensions = [
  {param:'stage',id:'base.stage',title:'阶段'},
  {param:'intent',id:'base.intent',title:'Base 操作'},
  {param:'cli',id:'base.cli',title:'CLI'},
] as const;

/** Query parameters only select recorded output; they must never dispatch plugin work. */
export function readDrilldown(search:string) {
  const params=new URLSearchParams(search);
  return {
    classifier:params.get('classifier')||'',
    status:['error','ok','unknown','running'].includes(params.get('status')||'')?params.get('status')!:'all',
    constraints:drilldownDimensions.flatMap(d=>{
      const value=params.get(d.param);
      return value&&value!=='all'?[{...d,value:value==='unknown'||value==='not_applicable'?value:'value:'+value}]:[];
    }),
  };
}

/** Batch buckets are predicates over existing assignments, never literal facet values. */
export function drilldownMatch(spanId:string,selected:string,assignments:Map<string,FacetAssignment>|undefined){
  const assignment=assignments?.get(spanId);
  if(selected==='value:__unclassified__')return assignments===undefined;
  if(selected==='value:__unknown__')return assignments!==undefined&&(!assignment||assignment.status!=='assigned'||!assignment.values.length);
  if(selected==='value:__multiple__')return assignment?.status==='assigned'&&assignment.values.length>1;
  if(selected==='all')return true;
  if(selected==='unknown')return !assignment||assignment.status==='unknown';
  if(selected==='not_applicable')return assignment?.status==='not_applicable';
  return assignment?.status==='assigned'&&assignment.values.includes(selected.slice('value:'.length));
}
