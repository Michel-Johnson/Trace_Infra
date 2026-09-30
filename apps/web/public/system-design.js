import {scenarios, createScienceScenario} from './system-guide-data.js';
import {experimentConditions, runReplenishmentExperiment} from './system-guide-experiments.js';
// Search stays inside this synthetic corpus. The optional rule simulator runs
// only on an explicit click; this page never dispatches model or plugin work.
const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = (name, cls = '') => `<img class="icon ${cls}" src="/guide-icons/${name}.svg" alt="" width="18" height="18">`;
const seconds = value => Number.isFinite(value) ? `${Number(value.toFixed(1))} 秒` : '未知';
const tokens = value => Number.isFinite(value) ? `${value.toLocaleString('zh-CN')} tokens` : 'token 未知';
const number = value => Number.isFinite(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits:2}) : '未知';
const percentage = value => Number.isFinite(value) ? `${(value*100).toFixed(1)}%` : '未知';
const kindNames = {read:'Read',bash:'Bash',write:'Write'};
const normalize = value => String(value ?? '').normalize('NFKC').toLocaleLowerCase('zh-CN');
const parseTerms = value => [...new Set(normalize(value).split(/[\s\p{P}\p{S}]+/u).filter(Boolean))];
let scene = scenarios[0];
let visible = scene.traces;
let selected = scene.traces.at(-1);
let activeTerms = [];
let experimentCondition = 'normal';
let experimentReport = null;
let experimentCount = 0;

function totals(trace) {
  const responses = trace.turns.flatMap(turn => turn.responses);
  const calls = responses.flatMap(response => response.calls);
  const durations = [...responses, ...calls].map(item => item.duration_s);
  const usage = responses.map(response => response.output_tokens);
  return {
    turns:trace.turns.length,
    time:durations.length && durations.every(Number.isFinite) ? durations.reduce((sum,value)=>sum+value,0) : null,
    tokens:usage.length && usage.every(Number.isFinite) ? usage.reduce((sum,value)=>sum+value,0) : null,
  };
}
function searchFields(trace) {
  const responses=trace.turns.flatMap(turn=>turn.responses);
  const calls=responses.flatMap(response=>response.calls);
  return [
    ['标题',trace.title],['标签',(trace.tags||[]).join(' ')],['摘要',trace.summary],
    ['原始任务',trace.turns.map(turn=>turn.query).join(' ')],
    ['模型响应',responses.map(response=>response.text).join(' ')],
    ['工具记录',calls.map(call=>`${call.name} ${typeof call.input==='string'?call.input:JSON.stringify(call.input)} ${typeof call.output==='string'?call.output:JSON.stringify(call.output)}`).join(' ')],
  ].map(([label,text])=>({label,text:normalize(text)}));
}
function matchReason(trace) {
  if(!activeTerms.length)return '';
  const fields=searchFields(trace);
  const matches=activeTerms.map(term=>`${fields.find(field=>field.text.includes(term))?.label||'记录'}「${term}」`);
  return `匹配：${matches.join('、')}`;
}
function renderTraceList() {
  $('trace-list').innerHTML=visible.map(trace=>`<button class="history-item ${selected?.id===trace.id?'selected':''}" data-trace="${escape(trace.id)}" aria-pressed="${selected?.id===trace.id}"><span class="history-dot" aria-hidden="true"></span><span><strong>${escape(trace.title)}</strong><span>${escape(trace.summary)}</span>${activeTerms.length?`<small class="search-match">${escape(matchReason(trace))}</small>`:''}</span></button>`).join('');
}
function callMarkup(call,response) {
  const resultLabel=call.status==='pending'?'待运行':call.status==='error'?'错误':'返回';
  return `<details class="tool-call ${escape(call.kind)} ${call.status==='error'?'call-error':''}" id="call-${escape(call.id)}"><summary><span class="call-head"><span class="tool-kind">${call.skill?'<b class="skill-label">S</b>':escape(kindNames[call.kind]||'Tool')}</span><span class="tool-name">${escape(call.name)}</span><span class="call-time">执行 ${seconds(call.duration_s)}</span>${icon('down','disclosure-icon')}</span><span class="tool-result"><span>${resultLabel}</span><span class="result-text">${escape(call.output)}</span></span></summary><div class="tool-detail"><dl><dt>输入</dt><dd><pre>${escape(call.input)}</pre></dd><dt>${resultLabel}</dt><dd><pre>${escape(call.output)}</pre></dd></dl><p class="tool-model-time">此前模型响应 ${seconds(response.duration_s)} · ${tokens(response.output_tokens)}</p></div></details>`;
}
function renderTrace() {
  if(!selected){
    $('trace-summary').textContent='0 条';
    $('trace-summary').removeAttribute('title');
    $('trace-content').innerHTML='<div class="empty-state"><h3>没有匹配的示例</h3><p>这里按关键词检索当前场景。试试上方关键词，或显示全部示例。</p><button class="text-button" data-reset>显示全部示例</button></div>';
    $('annotation-content').innerHTML='<p class="muted">选择一条轨迹后查看标注。</p>';
    return;
  }
  const sum=totals(selected);
  $('trace-summary').textContent=`${sum.turns} 轮 · ${sum.time===null?'时长未知':seconds(sum.time)}`;
  $('trace-summary').title=`${scene.summaryNote} 总模型输出：${tokens(sum.tokens)}`;
  $('trace-content').innerHTML=selected.turns.map((turn,index)=>`<details class="turn" open><summary class="turn-summary">${icon('down','disclosure-icon')}<span>第 ${index+1} 轮</span></summary><div class="turn-body"><div class="event user-event"><span class="event-icon">${icon('user')}</span><div class="event-content"><div class="event-heading"><strong>用户</strong><span>原始任务</span></div><p class="query-text">${escape(turn.query)}</p></div></div>${turn.responses.map(response=>`<div class="event model-event" id="response-${escape(response.id)}"><span class="event-icon">${icon('model')}</span><div class="event-content"><div class="event-heading"><strong>模型响应</strong><span class="model-time">${seconds(response.duration_s)} · ${tokens(response.output_tokens)}</span></div><p class="response-text">${escape(response.text)}</p>${response.calls.length?`<div class="tools">${response.calls.map(call=>callMarkup(call,response)).join('')}</div>`:''}</div></div>`).join('')}</div></details>`).join('');
  const a=selected.annotation;
  $('annotation-content').innerHTML=`<div class="annotation-label">${escape(a.label)}</div><p class="annotation-source">来源：${escape(a.source)}</p><button class="evidence-link" data-evidence="all">展开证据链 ${icon('export')}</button><div class="evidence-links"><button data-evidence="rule">${icon('file')}<span><strong>规则版本</strong><small>标签定义与判断依据</small></span></button><button data-evidence="expert">${icon('file')}<span><strong>专家参照</strong><small>查看标注材料</small></span></button><button data-evidence="validation">${icon('file')}<span><strong>验证记录</strong><small>查看对应材料</small></span></button></div>`;
}
function renderOverview() {
  $('scenario-overview').innerHTML=`<div><p class="small-label">${escape(scene.role)}</p><h2>${escape(scene.title)}</h2><p class="scenario-purpose">${escape(scene.description)}</p><a class="text-link" href="#scene-analysis">${scene.id==='science'?'运行策略实验，比较结果':'查看整批统计'} ${icon('arrow')}</a></div><dl class="scenario-task"><dt>原始任务</dt><dd>${escape(scene.task)}</dd><dt>分析输入</dt><dd>${escape(scene.inputs)}</dd><dt>要看什么</dt><dd>${escape(scene.success)}</dd></dl>`;
  $('demo-source-note').textContent=scene.summaryNote;
  $('reset-demo').textContent='显示全部示例';
}
function renderSearchState() {
  $('search-scope').textContent=`${scene.tab} · 当前示例库 ${scene.traces.length} 条 · 关键词检索`;
  $('search-status').textContent=activeTerms.length
    ? `找到 ${visible.length} / ${scene.traces.length} 条 · 同时匹配 ${activeTerms.map(term=>`「${term}」`).join('、')}`
    : `显示全部 ${visible.length} 条示例 · 输入关键词后点击检索`;
  $('search-presets').classList.add('search-presets');
  $('search-presets').innerHTML=scene.presets.map((preset,index)=>`<button type="button" data-preset="${index}" aria-pressed="${activeTerms.length>0&&activeTerms.join(' ')===parseTerms(preset.query).join(' ')}">${escape(preset.label)}</button>`).join('');
}
function simulationTable(report) {
  return `<div class="result-table-scroll"><table class="result-table"><thead><tr><th>方法</th><th>需求满足率</th><th>缺货 / 件</th><th>报损 / 件</th><th>总成本 / 元</th><th>期末 / 在途库存</th><th>预算 / 发运超限</th></tr></thead><tbody>${report.rows.map(row=>`<tr><th scope="row"><button class="result-method" data-method="${escape(row.method_id)}">${escape(row.method_name)} ${icon('arrow')}</button></th><td>${percentage(row.fill_rate)}</td><td>${number(row.stockout_units)}</td><td>${number(row.waste_units)}</td><td>${number(row.total_cost)}</td><td>${number(row.ending_stock)} / ${number(row.in_transit_units)}</td><td>${number(row.budget_violations)} / ${number(row.capacity_violations)}</td></tr>`).join('')}</tbody></table></div>`;
}
function renderAnalysis() {
  if(scene.id==='science'){
    const condition=experimentConditions.find(item=>item.id===experimentCondition);
    $('scene-analysis').innerHTML=`<div class="analysis-heading"><div><p class="eyebrow">同一数据 · 三种方法</p><h2>换一个条件，运行补货实验</h2></div><span class="data-badge">${experimentReport?'本地计算 · 已运行 '+experimentCount+' 次':'未运行'}</span></div><div class="experiment-controls"><label for="experiment-condition">实验条件</label><select id="experiment-condition">${experimentConditions.map(item=>`<option value="${escape(item.id)}" ${item.id===experimentCondition?'selected':''}>${escape(item.name)}</option>`).join('')}</select><button type="button" class="primary" id="run-experiment">${experimentReport?'重新运行 3 种策略':'运行 3 种策略'} ${icon('arrow')}</button></div><p class="analysis-note">${escape(condition.description)} 数据为固定种子的 50 店 × 7 天合成样本。</p>${experimentReport?simulationTable(experimentReport):'<div class="experiment-pending"><strong>尚未运行</strong><p>点击运行后，本页实际计算三种启发式策略的结果。不会调用大模型。</p></div>'}${experimentReport?`<details class="simulation-details"><summary>费用构成与实验口径</summary><div class="result-table-scroll"><table class="result-table"><thead><tr><th>方法</th><th>采购 / 元</th><th>配送 / 元</th><th>调拨 / 元</th><th>持有 / 元</th><th>报损处理 / 元</th></tr></thead><tbody>${experimentReport.rows.map(row=>`<tr><th scope="row">${escape(row.method_name)}</th>${['procurement','delivery','transfer','holding','disposal'].map(key=>`<td>${number(row.cost_breakdown[key])}</td>`).join('')}</tr>`).join('')}</tbody></table></div><p>每日预算 ${number(experimentReport.budget)} 元，由采购、采购配送和店间调拨共用；发运额度 ${number(experimentReport.daily_dispatch_capacity)} 件，也由采购与调拨共用。</p><p>总成本计入尚在途的已采购库存，单列期末与在途库存；不含初始库存成本和缺货机会成本。这是 7 天有限窗口内的完整策略比较，不能把差异归因于某一个因素。</p></details>`:''}<p class="analysis-note">方法名称可点开对应轨迹。对话是示例，实验结果由规则模拟器计算；这不能证明模型训练效果或真实门店收益。</p>`;
    return;
  }
  const batch=scene.batchSummary;
  $('scene-analysis').innerHTML=`<div class="analysis-heading"><div><p class="eyebrow">从单条记录到整批观察</p><h2>${escape(batch.label)}</h2></div><span class="data-badge">合成统计 · 非实测</span></div><div class="result-table-scroll"><table class="result-table"><thead><tr>${batch.columns.map(column=>`<th>${escape(column.label)}</th>`).join('')}</tr></thead><tbody>${batch.rows.map(row=>`<tr>${batch.columns.map((column,index)=>index===0?`<th scope="row">${row.traceId?`<button class="result-method" data-batch-trace="${escape(row.traceId)}">${escape(row[column.key])} ${icon('arrow')}</button>`:escape(row[column.key])}</th>`:`<td>${escape(row[column.key])}</td>`).join('')}</tr>`).join('')}</tbody></table></div><p class="analysis-note">${escape(batch.note)}</p>`;
}
function render(){renderTraceList();renderTrace();renderSearchState();}
function selectScene(id){
  scene=scenarios.find(item=>item.id===id)||scenarios[0];
  visible=scene.traces;selected=scene.traces.find(trace=>trace.id===scene.defaultTraceId)||scene.traces.at(-1);activeTerms=[];
  $('search-input').value=scene.presets[0]?.query||'';
  document.querySelectorAll('[data-scene]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.scene===scene.id)));
  renderOverview();render();renderAnalysis();
}
function applyActiveFilter(){
  visible=scene.traces.filter(trace=>{const text=searchFields(trace).map(field=>field.text).join(' ');return activeTerms.every(term=>text.includes(term));});
  if(!visible.some(trace=>trace.id===selected?.id))selected=visible[0]||null;
  render();
}
function search(){
  activeTerms=parseTerms($('search-input').value);
  applyActiveFilter();
}
function resetSearch(){
  $('search-input').value='';activeTerms=[];visible=scene.traces;
  if(!selected||!visible.some(trace=>trace.id===selected.id))selected=visible.at(-1);
  render();
}
function refreshScienceScene(){
  const previousId=selected?.id;
  const science=createScienceScenario(experimentReport,experimentCondition);
  scenarios[scenarios.findIndex(item=>item.id==='science')]=science;
  scene=science;
  selected=science.traces.find(trace=>trace.id===previousId)||science.traces.at(-1);
  renderOverview();applyActiveFilter();renderAnalysis();
}
function openEvidence(part){
  if(!selected)return;
  const a=selected.annotation;
  const names={all:'标注与证据',rule:'规则版本',expert:'专家参照',validation:'验证记录'};
  const fields=part==='all'?['rule','expert','validation']:[part];
  $('evidence-dialog-title').textContent=names[part];
  $('evidence-dialog-body').innerHTML=`<p class="small-label">${escape(scene.summaryNote)}</p><h3>${escape(a.label)}</h3><p>${escape(selected.finding)}</p>${fields.map(field=>`<section class="evidence-item"><h4>${names[field]}</h4><p>${escape(a[field])}</p></section>`).join('')}<div class="evidence-origin"><span>来源：${escape(a.source)}</span><button class="primary" id="locate-evidence">${a.evidenceCallId?'定位原始调用':'定位响应记录'} ${icon('arrow')}</button></div>`;
  $('evidence-dialog').showModal();
}
function locateEvidence(){
  $('evidence-dialog').close();
  const target=selected.annotation.evidenceCallId?$(`call-${selected.annotation.evidenceCallId}`):$(`response-${selected.turns[0].responses[0].id}`);
  if(!target)return;
  let ancestor=target;
  while(ancestor){if(ancestor.tagName==='DETAILS')ancestor.open=true;ancestor=ancestor.parentElement;}
  target.classList.add('evidence-target');
  target.scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'center'});
  target.setAttribute('tabindex','-1');target.focus({preventScroll:true});
  setTimeout(()=>target.classList.remove('evidence-target'),3500);
}
document.querySelectorAll('[data-scene]').forEach(button=>button.addEventListener('click',()=>selectScene(button.dataset.scene)));
$('trace-list').addEventListener('click',event=>{const button=event.target.closest('[data-trace]');if(button){selected=visible.find(trace=>trace.id===button.dataset.trace);render();}});
$('annotation-content').addEventListener('click',event=>{const button=event.target.closest('[data-evidence]');if(button)openEvidence(button.dataset.evidence);});
$('evidence-dialog-body').addEventListener('click',event=>{if(event.target.closest('#locate-evidence'))locateEvidence();});
$('trace-content').addEventListener('click',event=>{if(event.target.closest('[data-reset]'))resetSearch();});
$('trace-search').addEventListener('submit',event=>{event.preventDefault();search();});
$('search-input').addEventListener('search',()=>{if(!$('search-input').value)search();});
$('reset-demo').addEventListener('click',resetSearch);
$('search-presets').addEventListener('click',event=>{const button=event.target.closest('[data-preset]');if(button){$('search-input').value=scene.presets[Number(button.dataset.preset)].query;search();}});
$('scene-analysis').addEventListener('change',event=>{
  if(event.target.id!=='experiment-condition')return;
  experimentCondition=event.target.value;experimentReport=null;
  refreshScienceScene();$('experiment-condition').focus();
});
$('scene-analysis').addEventListener('click',event=>{
  const batchTrace=event.target.closest('[data-batch-trace]');
  if(batchTrace){
    resetSearch();selected=scene.traces.find(trace=>trace.id===batchTrace.dataset.batchTrace)||selected;render();
    document.querySelector('.trace-heading').scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'});
    return;
  }
  if(event.target.closest('#run-experiment')){
    experimentReport=runReplenishmentExperiment(experimentCondition);experimentCount+=1;
    refreshScienceScene();$('run-experiment').focus();return;
  }
  const method=event.target.closest('[data-method]');
  if(method){
    resetSearch();selected=scene.traces.find(trace=>trace.method_id===method.dataset.method)||selected;render();
    const heading=document.querySelector('.trace-heading');
    heading.scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'});
  }
});
const protocolExamples={
reference:{kind:'trace_revision',id:'demo-trace-001',revision:1,digest:'sha256:…'},
annotation:{target:{trace_revision:'demo-trace-001@1',record_id:'call-003'},name:'strategy',value:'transfer-first',producer:{annotator:'strategy-demo',version:'1'},evidence_refs:['demo-evidence-001']},
evidence:{subject_ref:'strategy-demo@1',method:{type:'expert-comparison',version:'1'},evidence_refs:[{role:'rubric',ref:'demo-rules@1'},{role:'dataset',ref:'demo-dataset@1'},{role:'expert_labels',ref:'demo-labels@1'},{role:'predictions',ref:'demo-outputs@1'}],result_ref:'demo-report-001'}
};
$('protocol-code').textContent=JSON.stringify(protocolExamples.reference,null,2);
function bindPanels(key) {
  document.querySelectorAll(`[data-${key}]`).forEach(button=>button.addEventListener('click',()=>{
    const value=button.dataset[key];
    document.querySelectorAll(`[data-${key}]`).forEach(other=>other.setAttribute('aria-pressed',String(other===button)));
    if(key==='protocol'){$('protocol-code').textContent=JSON.stringify(protocolExamples[value],null,2);$('copy-status').textContent='';}
    else document.querySelectorAll(`[data-${key}-panel]`).forEach(panel=>panel.hidden=panel.dataset[`${key}Panel`]!==value);
  }));
}
['architecture','sequence','protocol'].forEach(bindPanels);
$('copy-protocol').addEventListener('click',async()=>{
  try{await navigator.clipboard.writeText($('protocol-code').textContent);$('copy-status').textContent='已复制示意结构';}
  catch{const range=document.createRange();range.selectNodeContents($('protocol-code'));const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);$('copy-status').textContent='已选中，请按 Ctrl/Cmd+C 复制';}
});
document.querySelectorAll('[data-close-dialog]').forEach(button=>button.addEventListener('click',()=>$(button.dataset.closeDialog).close()));
document.querySelectorAll('.diagram>a').forEach(link=>link.addEventListener('click',event=>{
  if(event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;
  event.preventDefault();const source=link.querySelector('img');
  $('diagram-dialog-title').textContent=link.closest('figure').querySelector('figcaption').textContent;
  $('diagram-dialog-image').src=source.src;$('diagram-dialog-image').alt=source.alt;$('diagram-dialog').showModal();
}));
$('print-document').addEventListener('click',()=>window.print());
const navLinks=[...document.querySelectorAll('.site-header nav a[href^="#"]')];
const anchors=navLinks.map(link=>document.querySelector(link.hash));
let pending=false;
function updateNav(){const active=[...anchors].reverse().find(section=>section.getBoundingClientRect().top<=160)||anchors[0];navLinks.forEach(link=>{if(link.hash==='#'+active.id)link.setAttribute('aria-current','location');else link.removeAttribute('aria-current');});pending=false;}
window.addEventListener('scroll',()=>{if(!pending){pending=true;requestAnimationFrame(updateNav);}},{passive:true});
selectScene('science');updateNav();
