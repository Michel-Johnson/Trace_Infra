import { Button, Table, Tooltip } from 'antd';
import type { Bundle, Row, Operation } from '../api/types';
import { callLabel, callTimeLabel, responseLabel, responseTime, seconds } from '../lib/format';

export type RenderContext = {
  bundle: Bundle; rows: { row:Row; index:number }[]; metric:'tool_ms'|'cycle_ms';
  colors:(operation:Operation)=>string[]; showLetters:boolean;
  inspect:(row:Row,index:number)=>void;
};
const thresholds=[100,1000,5000,15000,60000];
const letters={read:'R',write:'W',bash:'B',skill:'S',human:'?',other:'·'};
export function CallTooltip({row,index}:{row:Row;index:number}) {
  return <div className="call-tooltip"><strong>{callLabel(row)} #{index+1}</strong><div><span>{callTimeLabel(row)}</span><b>{seconds(row.tool_ms)}</b></div><div><span>{responseLabel(row)}</span><b>{responseTime(row)}</b></div><small>{row.operation==='human'&&'交互调用整段，未拆分纯等待。'}{row.response_basis==='interval_estimate'&&'前置间隔含调度、传输与漏采。'}点击查看详情</small></div>;
}
function GridRenderer({bundle,rows,metric,colors,showLetters,inspect}:RenderContext) {
  return <div className="trace-cells" aria-label={`${bundle.run.harness} ${bundle.run.model}工具调用`}>{rows.map(({row,index})=>{
    const duration=row[metric],level=duration==null?0:thresholds.filter(n=>duration>=n).length;
    const hasSkill=!!row.skill||row.operation==='skill';
    const label=`${bundle.run.harness} ${callLabel(row)} #${index+1}，${callTimeLabel(row)} ${seconds(row.tool_ms)}，${responseLabel(row)} ${responseTime(row)}${row.status==='error'?'，错误':''}`;
    return <Tooltip key={row.span_id} mouseEnterDelay={0.15} title={<CallTooltip row={row} index={index}/>}><button type="button" className={'trace-cell'+(duration==null?' missing':'')+(row.status==='error'?' failed':'')} style={{backgroundColor:duration==null?'#eeeeee':colors(row.operation)[level],color:level>=3?'#fff':'#222'}} aria-label={label} onClick={()=>inspect(row,index)}>
      {showLetters&&row.operation!=='skill'?letters[row.operation]:''}{hasSkill&&<span className="skill-marker" aria-hidden="true">S</span>}{row.status==='error'&&<span className="error-marker" aria-hidden="true">!</span>}
    </button></Tooltip>;
  })}</div>;
}
function ListRenderer({rows,inspect,colors}:RenderContext) {
  return <Table className="workspace-table trace-list-renderer" rowKey={item=>item.row.span_id} size="small" dataSource={rows} pagination={{pageSize:15,hideOnSinglePage:true}} columns={[
    {title:'#',dataIndex:'index',width:55,render:i=>i+1},
    {title:'调用',key:'call',render:(_,item)=><Button type="text" onClick={()=>inspect(item.row,item.index)}><i className="color-dot" style={{background:colors(item.row.operation)[3]}}/>{callLabel(item.row)}{item.row.skill?' · S':''}</Button>},
    {title:'执行用时',key:'duration',render:(_,item)=>seconds(item.row.tool_ms)},
    {title:'前置模型响应',key:'response',responsive:['md'],render:(_,item)=>responseTime(item.row)},
    {title:'状态',key:'status',render:(_,item)=>item.row.status==='error'?'错误':item.row.status==='ok'?'完成':'未知'},
  ]}/>;
}

// Only bundled implementations can mount in the current frontend.
export const renderers:Record<string,(context:RenderContext)=>React.ReactNode>={'trace.grid':GridRenderer,'trace.list':ListRenderer};
