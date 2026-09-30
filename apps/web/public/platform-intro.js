// Local, synthetic product demonstration. No model, connector or platform calls.
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const esc = (value) => String(value).replace(/[&<>"']/g, (character) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
const icon = (name, size = 14) => `<img src="/guide-icons/${name}.svg" width="${size}" height="${size}" alt="">`;
const seconds = (value) => value == null ? '未知' : `${value.toFixed(1)}s`;
const scenes = {
  science: {
    prompt: '模型为什么总用同一种补货方法？', label: '01 / 科学问题 · 模型策略',
    title: '同样的门店，能不能换种解法？',
    context: '为 50 家门店制定未来 7 天的补货计划，考虑库存、保鲜期、预算和配送限制。把不同策略的执行过程放在一起，寻找值得进一步实验的方法。',
    query: '帮我统筹 50 家门店未来 7 天的补货。预算不变，尽量减少缺货和生鲜损耗。',
    reasoning: '先对齐各店库存与需求，再生成一种满足约束的方案，保存方法和结果。',
    variants: [
      {label:'固定阈值补货', harness:'Codex', note:'按上下限触发采购', method:'threshold', result:'已生成按库存上下限触发的补货清单。'},
      {label:'需求优先分配', harness:'Claude Code', note:'优先补给需求缺口', method:'demand_priority', result:'已生成优先覆盖需求缺口的采购分配。'},
      {label:'先调拨，再采购', harness:'Pi', note:'先利用门店间余量', method:'transfer_first', result:'已生成跨店调拨与补充采购的组合方案。'},
    ],
    findingTitle:'先看策略差异，<br>再比较真实效果。',
    findingCopy:'检索类似任务，按业务方法标注策略。固定相同输入，再用独立规则比较缺货、损耗和成本。',
    tags:['阈值策略','需求分配','门店调拨'], methodName:'策略分类器 · 示例',
    evidenceTitle:'策略比较需要哪些证据？',
    evidence:[['原始任务','50 家门店 × 7 天'],['比较输入','相同库存、需求、预算与配送条件'],['方法记录','策略、参数、模型与工具版本'],['验证指标','缺货、损耗、成本及约束满足情况']],
    evidenceNote:'本页只演示三种策略如何被记录和查找，不提供真实模型排名或实验结论。',
    keywords:['补货','门店','策略','模型','库存','调拨','科学','需求','训练','codex','claude','pi'],
    calls(variant) { return [
      {kind:'read', skill:true, name:'读取补货规划 Skill', model:6.8, execution:3.2, preview:'读取任务约束和检查规则', input:'skills/replenishment/SKILL.md', output:{constraints:['保鲜期','采购预算','配送额度'], unit:'门店 × 日期 × 商品'}},
      {kind:'read', name:'读取库存与需求', model:4.2, execution:1.6, preview:'50 家门店 · 7 天 · 相同输入版本', input:'inventory.read(snapshot="example-stock-v1")', output:{stores:50, days:7, input_version:'example-stock-v1', synthetic:true}},
      {kind:'bash', name:`计算${variant.label}`, model:7.4, execution:14.2, preview:`方法已记录：${variant.method}`, input:`python plan.py --method ${variant.method} --input example-stock-v1`, output:{method:variant.method, plan_ref:'example-plan-003', comparison:'待独立实验验证'}},
      {kind:'write', name:'保存方案与执行依据', model:5.6, execution:4.2, preview:'结果关联数据版本、方法与原始调用', input:'artifacts.save(plan, inputs, method)', output:{plan_ref:'example-plan-003', source:'example-trace-003', validation:'尚未比较策略优劣'}},
    ]; },
  },
  system: {
    prompt:'为什么物业巡检 Skill 最近调用减少了？', label:'02 / 系统优化 · 物业设施巡检',
    title:'巡检 Skill 用得少了，问题在哪？',
    context:'集团用摄像头截图与传感器数据辅助日常巡检。结合调用轨迹、授权的巡检计划和工单，区分使用覆盖变化与接口异常，定位值得排查的楼宇。',
    query:'检查 C 栋的设备间和公共区域。读取可用的摄像头及传感器，标出需要人工复核的点位。',
    reasoning:'对齐今天的巡检计划，读取相关点位数据，再记录异常和未能检查的区域。',
    variants:[
      {label:'C 栋设备巡检', harness:'Cursor', note:'摄像头返回 401', building:'C', cameraError:true, result:'传感器数据已读；摄像头鉴权失败的区域已标记为待人工复核。'},
      {label:'B 栋设施巡检', harness:'Claude Code', note:'读取摄像头与传感器', building:'B', cameraError:false, result:'已读取本次巡检的摄像头截图和传感器数据，生成复核清单。'},
      {label:'A 栋日常巡检', harness:'Codex', note:'读取摄像头与传感器', building:'A', cameraError:false, result:'已保存巡检记录，并关联本次计划与点位数据。'},
    ],
    findingTitle:'调用少了，<br>覆盖也变了吗？',
    findingCopy:'在这组合成样例里，C 栋的调用覆盖下降最多，401 返回也增多。这提供排查方向，还不能直接证明原因。',
    metrics:[['Skill 调用','900 → 600'],['计划调用覆盖','80% → 50%'],['已知状态失败率','6.7% → 11.7%']], methodName:'使用情况统计 · 合成样例',
    evidenceTitle:'这份统计的分母从哪里来？',
    evidence:[['周期','两个连续、同长度的 7 天周期'],['Skill 调用','900 → 600；下降 33.3%'],['授权巡检计划','每个周期均有 1,000 项计划巡检'],['发起过调用的计划项','800 → 500；按计划 ID 去重'],['状态已知的失败调用','60 / 900 → 70 / 600'],['主要变化位置','C 栋：调用覆盖 240 → 60 项']],
    evidenceNote:'全部为已核对算术的合成数据。调用覆盖不代表巡检完成率；前后差异不等于因果结论。真实连接器与 Sancho 尚在原型设计阶段。',
    keywords:['物业','巡检','skill','摄像头','传感器','调用','401','楼宇','工单','覆盖','系统'],
    calls(variant) { return [
      {kind:'read', skill:true, name:'读取设施巡检 Skill', model:8.3, execution:2.6, preview:'加载点位读取和异常标记规则', input:'skills/facility-inspection/SKILL.md', output:{inputs:['摄像头','传感器','巡检计划'], unavailable:'标记待人工复核'}},
      {kind:'read', name:'读取授权巡检计划', model:4.5, execution:1.8, preview:`${variant.building} 栋 · 本次计划快照`, input:`inspection_plan.query(building="${variant.building}")`, output:{building:variant.building, source:'用户授权连接 · 示例', snapshot:'example-plan-input-v1'}},
      {kind:'bash', name:'查询点位传感器', model:6.1, execution:4.7, preview:'取得温度、水位和设备状态', input:`sensors.query(building="${variant.building}")`, output:{building:variant.building, temperature:'正常范围', water_level:'正常范围', synthetic:true}},
      {kind:'bash', name:'读取摄像头截图', model:5.8, execution:1.4, error:variant.cameraError, preview:variant.cameraError ? '401 · 当前请求鉴权失败' : '截图已读取，保留原始附件引用', input:`camera.snapshot(building="${variant.building}", point="equipment-room")`, output:variant.cameraError ? {status:401, error:'unauthorized', next:'保留失败记录，标记待复核'} : {status:200, attachment_ref:'example-camera-frame', next:'按规则检查'}},
      {kind:'write', name:'保存巡检与复核记录', model:7.2, execution:3.1, preview:'成功、失败和缺失点位分别保存', input:'inspection_report.save(input_refs, observations)', output:{building:variant.building, camera:variant.cameraError ? '待人工复核' : '已取得截图', report_ref:'example-report-017'}},
    ]; },
  },
  geo: {
    prompt:'怎样让我的旅行社出现在 AI 的行程推荐里？', label:'03 / 商业研究 · 旅行 GEO',
    title:'被 AI 搜到，为什么没有被推荐？',
    context:'一家旅行社想提高在旅行规划中的曝光。查看不同需求下的搜索、候选与最终引用，研究哪些内容被使用，再通过实验验证内容改动的效果。',
    query:'带父母去云南玩 5 天，不自驾。帮我安排轻松一点的路线，也看看合适的当地服务。',
    reasoning:'先确认行程限制，再检索路线和服务信息，比较候选后说明推荐依据。',
    variants:[
      {label:'父母同行 · 5 天', harness:'Codex', note:'节奏轻松 · 不自驾', term:'云南 父母 5天 轻松 路线', query:'带父母去云南玩 5 天，不自驾。帮我安排轻松一点的路线，也看看合适的当地服务。', result:'已生成轻松的 5 天路线。部分被检索内容没有进入最终引用。'},
      {label:'亲子出游 · 3 天', harness:'Claude Code', note:'儿童友好 · 短途', term:'云南 亲子 3天 当地服务', query:'带 6 岁孩子去云南玩 3 天，想要儿童友好的短途路线，帮我看看当地接送和游玩服务。', result:'已生成亲子路线，并保存候选服务与最终引用的对应关系。'},
      {label:'小众徒步 · 4 天', harness:'Pi', note:'当地向导 · 路线保障', term:'云南 徒步 4天 当地向导', query:'安排云南 4 天小众徒步，比较路线难度和保障条件，并找合适的当地向导服务。', result:'已生成徒步路线，并保留选用当地服务信息的来源。'},
    ],
    findingTitle:'被搜到，<br>不等于被推荐。',
    findingCopy:'把检索命中、进入候选和最终引用分开看。结合旅社的内容版本，寻找值得测试的改进机会。',
    tags:['检索命中','候选服务','最终引用'], methodName:'引用路径标注 · 示例',
    evidenceTitle:'曝光研究需要哪些记录？',
    evidence:[['用户需求','出行人群、天数、交通与预算限制'],['检索过程','搜索词、返回内容及内容版本'],['候选服务','哪些服务被读取、比较或排除'],['最终引用','回答实际引用了哪些来源'],['后续实验','固定条件后比较内容改动的效果']],
    evidenceNote:'旅社与行程均为合成示例。本页不承诺 GEO 曝光提升，也不把内容被检索当作转化或因果证据。',
    keywords:['geo','旅行','旅社','云南','行程','推荐','曝光','旅游','引用','商业'],
    calls(variant) { return [
      {kind:'read', skill:true, name:'读取旅行规划 Skill', model:4.5, execution:2.1, preview:'记录人群、交通和行程约束', input:'skills/travel-planning/SKILL.md', output:{keep:['用户约束','搜索过程','引用来源']}},
      {kind:'bash', name:'检索路线与当地服务', model:6.2, execution:8.4, preview:'返回路线资料和服务介绍', input:`search(query="${variant.term}")`, output:{sources:['路线资料 A','云间旅行 · 服务页','出行说明 B'], synthetic:true}},
      {kind:'read', name:'读取并比较候选内容', model:9.4, execution:3.7, preview:'记录候选内容与版本', input:'sources.read(selected_candidates)', output:{retrieved:'云间旅行 · 服务页', version:'example-content-v2', final_citation:'未引用；原因需结合完整任务分析'}},
      {kind:'write', name:'保存行程与引用', model:12.8, execution:2.2, preview:'行程、引用和未采用的候选分别记录', input:'artifacts.save(itinerary, source_refs)', output:{itinerary:'example-itinerary-005', citations:['路线资料 A','出行说明 B'], candidate_refs:['云间旅行 · 服务页']}},
    ]; },
  },
};

let sceneKey = 'science';
let variantIndex = 0;
let activeCalls = [];
let dialogOpener = null;
const dialog = $('#detail-dialog');

function setTabState(attribute, selected, panel) {
  $$(`[${attribute}]`).forEach((button) => {
    const active = button.getAttribute(attribute) === selected;
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
    if (active) $(panel).setAttribute('aria-labelledby', button.id);
  });
}

function openDetail(title, subtitle, body, note) {
  dialogOpener = document.activeElement;
  $('#detail-body').innerHTML = `<h2 id="detail-title">${esc(title)}</h2><p class="detail-subtitle">${esc(subtitle)}</p>${body}<p class="detail-note">${esc(note)}</p>`;
  dialog.showModal();
  $('#close-detail').focus();
}

function showCall(index) {
  const call = activeCalls[index];
  if (!call) return;
  openDetail(call.name, `${scenes[sceneKey].variants[variantIndex].harness} · 示例调用 ${index + 1}`, `<div class="detail-timings"><div><span>调用前模型输出</span><strong>${seconds(call.model)}</strong></div><div><span>工具执行</span><strong>${seconds(call.execution)}</strong></div></div><div class="detail-section"><h3>调用了什么</h3><code>${esc(call.input)}</code></div><div class="detail-section"><h3>返回了什么</h3><pre>${esc(JSON.stringify(call.output,null,2))}</pre></div><div class="detail-section"><h3>如何引用这一步</h3><code>example-${esc(sceneKey)}-${variantIndex + 1} / call-${String(index + 1).padStart(3,'0')}</code></div>`, '合成调用与计时，仅用于说明展示方式。真实记录应保留来源、版本及缺失信息。');
}

function showEvidence() {
  const scene = scenes[sceneKey];
  openDetail(scene.evidenceTitle, scene.methodName, `<div class="detail-section">${scene.evidence.map(([key,value]) => `<div class="detail-evidence-row"><span>${esc(key)}</span><strong>${esc(value)}</strong></div>`).join('')}</div>`, scene.evidenceNote);
}

function renderTrace() {
  const scene = scenes[sceneKey];
  const variant = scene.variants[variantIndex];
  activeCalls = scene.calls(variant);
  $('#runs').innerHTML = scene.variants.map((run, index) => `<button class="run-choice" type="button" data-run="${index}" aria-pressed="${index === variantIndex}" aria-label="查看${esc(run.label)}轨迹"><small>TRACE ${String(index+1).padStart(3,'0')} · ${esc(run.harness)}</small><strong>${esc(run.label)}</strong><span class="run-meta">${esc(run.note)}</span></button>`).join('');
  $('#trace-name').textContent = `${variant.harness} · ${variant.label}`;
  const query = variant.query || (sceneKey === 'system' ? scene.query.replace('C 栋',`${variant.building} 栋`) : scene.query);
  $('#trace-events').innerHTML = `<div class="trace-event"><div class="event-icon">${icon('user')}</div><div class="event-body"><span class="event-label">用户 Query</span><p class="user-query">${esc(query)}</p></div></div><div class="trace-event"><div class="event-icon">${icon('model')}</div><div class="event-body"><span class="event-label">模型响应</span><p>${esc(scene.reasoning)}</p></div></div>${activeCalls.map((call,index) => `<div class="trace-event tool-event"><div class="event-icon">${icon(call.kind === 'bash' ? 'code' : call.kind)}</div><div class="event-body"><button class="tool-row ${call.error ? 'error' : ''}" data-call="${index}" data-kind="${call.kind}" type="button" aria-label="查看调用详情：${esc(call.name)}"><span class="tool-type">${call.skill ? 'S · Read' : call.kind === 'bash' ? 'Bash' : call.kind === 'write' ? 'Write' : 'Read'}</span><span class="tool-name">${esc(call.name)}</span><span class="tool-time">${seconds(call.execution)}</span></button><p class="tool-result">${esc(call.preview)}</p></div></div>`).join('')}<div class="trace-event final-event"><div class="event-icon">${icon('check')}</div><div class="event-body"><span class="event-label">模型返回</span><p>${esc(variant.result)}</p></div></div>`;
  $('#call-grid').innerHTML = activeCalls.map((call,index) => `<button type="button" class="call-square" data-kind="${call.kind}" data-call="${index}" aria-label="${esc(call.name)}，模型输出 ${seconds(call.model)}，执行 ${seconds(call.execution)}" title="${esc(call.name)}｜模型输出 ${seconds(call.model)} · 执行 ${seconds(call.execution)}">${call.skill ? 'S' : ''}</button>`).join('');
  $('#finding').innerHTML = `<div class="finding-mark">${esc(scene.methodName)}</div><h3>${scene.findingTitle}</h3><p class="finding-copy">${esc(scene.findingCopy)}</p>${scene.tags ? `<div class="finding-tags">${scene.tags.map(tag=>`<span>${esc(tag)}</span>`).join('')}</div>` : `<div class="finding-metrics">${scene.metrics.map(([key,value])=>`<div><span>${esc(key)}</span><strong>${esc(value)}</strong></div>`).join('')}</div>`}<button class="evidence-button" type="button" id="show-evidence">看看证据${icon('arrow')}</button>`;
  $$('[data-run]').forEach(button => button.addEventListener('click',()=>{variantIndex=Number(button.dataset.run);renderTrace();}));
  $$('[data-call]').forEach(button => button.addEventListener('click',()=>showCall(Number(button.dataset.call))));
  $('#show-evidence').addEventListener('click',showEvidence);
}

function renderScene(key, {resetQuery=true}={}) {
  sceneKey=key;variantIndex=0;
  const scene=scenes[key];
  setTabState('data-scene',key,'#scene-panel');
  $('#scene-panel').hidden=false;$('#empty-search').hidden=true;
  $('#story-label').textContent=scene.label;
  $('#experience-title').textContent=scene.title;
  $('#story-context').textContent=scene.context;
  $('#search-feedback').textContent='';
  if(resetQuery) $('#demo-query').value=scene.prompt;
  renderTrace();
}

$$('[data-scene]').forEach(button=>button.addEventListener('click',()=>renderScene(button.dataset.scene)));
$('#demo-search').addEventListener('submit',event=>{
  event.preventDefault();
  const query=$('#demo-query').value.trim().toLowerCase();
  const scored=Object.entries(scenes).map(([key,scene])=>({key,score:scene.keywords.reduce((n,word)=>n+(query.includes(word)?1:0),0)})).sort((a,b)=>b.score-a.score);
  if(query && scored[0].score){
    renderScene(scored[0].key,{resetQuery:false});
    $('#search-feedback').textContent=`已打开「${scenes[sceneKey].label.split(' · ')[1]}」内置示例，可点击轨迹和工具查看。`;
  }else{
    $('#scene-panel').hidden=true;$('#empty-search').hidden=false;
    $('#search-feedback').textContent=query?'未匹配内置示例。':'请输入一个问题，或选择一个场景。';
  }
  $('#experience').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'start'});
});
$('#reset-scene').addEventListener('click',()=>renderScene(sceneKey));
$('#show-examples').addEventListener('click',()=>renderScene(sceneKey));

const entries={
  web:{title:'平台工作台',state:'网页操作',copy:'选中一批巡检轨迹，固定数据范围，运行你选择的统计插件。结果留在项目里，之后还可以继续分析。',steps:['选数据','运行方法','查看报告']},
  agent:{title:'你自己的 Agent',state:'公开接口接入',copy:'把同一个选集引用交给自己的 Agent。通过 API 或工具读取完整数据、运行程序，再将报告和来源写回平台。',steps:['读取选集','执行分析','回写结果']},
  sancho:{title:'Sancho · 内置助手',state:'原型设计',copy:'“解释这份巡检报告，带我看看 C 栋的原始调用。”Sancho 读取报告和获准访问的材料，协助你追查证据。',steps:['提出问题','读取证据','继续工作']},
};
function renderEntry(key){
  setTabState('data-entry',key,'#entry-panel');
  const entry=entries[key];
  $('#entry-panel').innerHTML=`<div class="entry-panel-head"><span>${esc(entry.title)}</span><span>${esc(entry.state)} · 使用方式演示</span></div><p class="entry-copy">${esc(entry.copy)}</p><div class="entry-steps">${entry.steps.map(esc).join(icon('right',11))}</div><div class="saved-result"><div>同一份巡检分析报告<span>选集 example-042 · 报告 example-017</span></div><button type="button" id="entry-result">查看示例${icon('arrow')}</button></div>`;
  $('#entry-result').addEventListener('click',()=>openDetail('一份结果，三个入口接着用','项目 / 物业巡检分析 · 合成示例',`<div class="detail-section"><div class="detail-evidence-row"><span>固定选集</span><strong>example-042</strong></div><div class="detail-evidence-row"><span>统计任务</span><strong>example-job-008</strong></div><div class="detail-evidence-row"><span>分析报告</span><strong>example-017</strong></div><div class="detail-evidence-row"><span>输入材料</span><strong>轨迹选集 + 授权计划快照</strong></div></div><div class="detail-section"><h3>可以怎样接续</h3><p>用户选数据，自己的 Agent 做分析，Sancho 解释结果。切换入口后继续引用同一份材料，无需重新计算。</p></div>`,'这是目标使用方式演示。统一 Sancho 与授权连接器尚未接入，本页没有发起真实任务。'));
}
$$('[data-entry]').forEach(button=>button.addEventListener('click',()=>renderEntry(button.dataset.entry)));

const lenses={
  ops:{title:'哪个接口值得先排查？',text:'关联同一楼宇、点位和时间范围内的失败调用，检查授权变更与服务状态。401 是观察到的返回，具体原因需要进一步核实。',tags:['接口鉴权','按楼宇切片']},
  training:{title:'模型怎样处理执行失败？',text:'查看后续是否正确识别错误、保留未完成项、采用有依据的恢复方式。用经过验证的规则标注策略，供训练样本选择参考。',tags:['异常恢复策略','标注与验证']},
  business:{title:'哪些区域没有完成检查？',text:'结合授权巡检计划、点位与复核记录，确认检查覆盖和待办。一次接口失败不直接等于一次巡检任务失败。',tags:['计划关联','待人工复核']},
};
function renderLens(key){
  setTabState('data-lens',key,'#lens-panel');
  const lens=lenses[key];
  $('#lens-panel').innerHTML=`<h3>${esc(lens.title)}</h3><p class="lens-text">${esc(lens.text)}</p><div class="lens-tags">${lens.tags.map(tag=>`<span>${esc(tag)}</span>`).join('')}</div>`;
}
$$('[data-lens]').forEach(button=>button.addEventListener('click',()=>renderLens(button.dataset.lens)));

// Arrow navigation and roving focus keep every selector usable without a mouse.
const sceneLayout = matchMedia('(max-width: 850px)');
const updateSceneOrientation = () => $('.scene-tabs').setAttribute('aria-orientation', sceneLayout.matches ? 'horizontal' : 'vertical');
updateSceneOrientation();
sceneLayout.addEventListener('change', updateSceneOrientation);
$$('[role=tablist]').forEach(tablist=>tablist.addEventListener('keydown',event=>{
  const vertical=tablist.getAttribute('aria-orientation')==='vertical';
  const previous=vertical?'ArrowUp':'ArrowLeft',next=vertical?'ArrowDown':'ArrowRight';
  if(![previous,next,'Home','End'].includes(event.key))return;
  const buttons=[...tablist.querySelectorAll('[role=tab]')];
  const index=buttons.indexOf(document.activeElement);if(index<0)return;
  event.preventDefault();
  const target=event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key===next?1:-1)+buttons.length)%buttons.length;
  buttons[target].click();buttons[target].focus();
}));
$('#close-detail').addEventListener('click',()=>dialog.close());
dialog.addEventListener('click',event=>{if(event.target===dialog){const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)dialog.close();}});
dialog.addEventListener('close',()=>{if(dialogOpener instanceof HTMLElement && dialogOpener.isConnected)dialogOpener.focus();});
window.addEventListener('scroll',()=>$('.nav-shell').classList.toggle('scrolled',window.scrollY>12),{passive:true});
renderScene('science');
if('IntersectionObserver' in window && !matchMedia('(prefers-reduced-motion: reduce)').matches){
  document.documentElement.classList.add('js-ready');
  const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting){entry.target.classList.add('visible');observer.unobserve(entry.target);}}),{threshold:.08});
  $$('.reveal').forEach(element=>observer.observe(element));
}
