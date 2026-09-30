import type { Row,Operation } from '../api/types';
export type OperationFilter={types:Operation[];skill:string;status:string};
export function detailMatch(row:Row,filter:OperationFilter) {
  const relation=row.skill?.action||(row.operation==='skill'?'invoke':'none');
  return (filter.skill==='all'||(filter.skill==='any'?relation!=='none':relation===filter.skill))&&(filter.status==='all'||(filter.status==='missing'?row.tool_ms==null:row.status==='error'));
}
export function operationMatch(row:Row,filter:OperationFilter) {
  return (!filter.types.length||filter.types.includes(row.operation))&&detailMatch(row,filter);
}
export type FacetAssignment={status:string;values:string[]};
export function facetMatch(spanId:string,selected:string,assignments:Map<string,FacetAssignment>|undefined) {
  if(selected==='all')return true;
  const assignment=assignments?.get(spanId);
  if(selected==='unknown')return !assignment||assignment.status==='unknown';
  if(selected==='not_applicable')return assignment?.status==='not_applicable';
  return assignment?.status==='assigned'&&assignment.values.includes(selected.slice('value:'.length));
}
export const slicers={'trace.operations':operationMatch,'trace.facets':facetMatch};
