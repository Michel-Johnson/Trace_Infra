/* Offline explanatory examples. They never dispatch analysis or execute trace content. */
const lenses = {
  base: { owner: 'Base 阶段插件', answer: '已建表；第 2 轮调整权限失败。', evidence: '建表返回成功，权限调用返回拒绝。业务标签由插件解释，不改写原始记录。', property: 'base.stage = permission', labels: { plan: 'Plan', skill: '读取与准备', prepare: 'Spec', table: 'Table', created: '交付', check: '检查', permission: 'Permission', reply: '待处理' }, highlight: ['permission'] },
  latency: { owner: '耗时分析插件', answer: '44 秒中，模型占 32 秒（73%）；最长请求是第 2 轮的 18 秒。', evidence: '5 次模型请求：6 + 4 + 2 + 18 + 2 = 32 秒。3 次工具执行：1 + 8 + 3 = 12 秒。此演示无并行和等待；消息展示不另计时。', property: 'latency.focus = model-request-04', labels: { plan: '6 秒', skill: '1 秒', prepare: '4 秒', table: '8 秒', created: '2 秒', check: '最长请求', permission: '3 秒', reply: '2 秒' }, highlight: ['check'] },
  quality: { owner: '业务验收 · 演示', answer: '第 1 轮建表待核验；第 2 轮协作需求未完成。', evidence: '分别对齐两轮用户要求。建表成功还需回读产物；第二轮权限失败不能直接否定第一轮。', property: 'acceptance.turn-1 = unverified', labels: { plan: '需求规划', skill: '读取技能', prepare: '字段设计', table: '待核验', created: '第 1 轮交付', check: '新增需求', permission: '未完成', reply: '说明原因' }, highlight: ['table', 'permission'] },
  training: { owner: '训练选样 · 规划演示', answer: '可按训练目标选取不同片段，先审核上下文和执行结果。', evidence: '选样需要固定轨迹版本并记录审核。第一轮可以关注建表过程，第二轮可以关注权限失败后的回复。被选中不代表已用于训练。', property: 'training.candidate = needs-review', labels: { plan: '上下文', skill: '技能依赖', prepare: '候选片段', table: '候选片段', created: '待审核', check: '上下文', permission: '失败样本', reply: '失败后回复' }, highlight: ['table', 'permission', 'reply'] },
};
function selectLens(button) {
  const lens = lenses[button.dataset.lens];
  for (const candidate of document.querySelectorAll('[data-lens]')) candidate.setAttribute('aria-pressed', String(candidate === button));
  for (const [id, value] of Object.entries({ 'lens-owner': lens.owner, 'lens-answer': lens.answer, 'lens-evidence': lens.evidence, 'lens-property': lens.property })) document.getElementById(id).textContent = value;
  for (const row of document.querySelectorAll('[data-event]')) {
    row.querySelector('.event-label').textContent = lens.labels[row.dataset.event];
    row.classList.toggle('focused', lens.highlight.includes(row.dataset.event));
  }
}
for (const button of document.querySelectorAll('[data-lens]')) button.addEventListener('click', () => selectLens(button));
const initialLens = document.querySelector('[data-lens][aria-pressed="true"]');
if (initialLens) selectLens(initialLens);
const steps = {
  receive: ['平台核心', '接收真实记录，说明缺了什么。', '按当前服务支持的格式校验。模型、工具、时间、用量可以分别缺失；不知道的值保留未知，不能补成零。'],
  store: ['平台核心', '原件留下来，修复另存一版。', '保存输入原文与摘要。补采、修复形成新的版本，旧内容仍可回读；分析结果可以指向准确的输入版本。'],
  index: ['平台核心', '让人和 Agent 都能找到需要的记录。', '建立可重建的查询索引，保留调用顺序和来源位置。按条件分页读取，避免每次都搬走整条长轨迹。'],
  select: ['平台核心', '固定这次要处理哪一批数据。', '将运行、版本和摘要组成选集。之后数据有新版本，这次分析的输入不会悄悄变化。'],
  analyze: ['平台负责执行，插件提供业务规则', '明确触发后，才开始分析。', '选择插件及配置，创建任务。官方 Worker 或接入的远程 Agent 读取固定输入，把分类、报告等作为新的产物提交。'],
  review: ['平台核心', '从一句结论，回到它的证据。', '查看使用的输入、插件版本、配置和执行记录。业务结论引用具体轮次与调用；新的解释追加保存，原始记录保持不变。'],
};
for (const button of document.querySelectorAll('[data-step]')) button.addEventListener('click', () => {
  for (const candidate of document.querySelectorAll('[data-step]')) candidate.setAttribute('aria-pressed', String(candidate === button));
  const step = steps[button.dataset.step];
  ['flow-kind', 'flow-title', 'flow-text'].forEach((id, index) => { document.getElementById(id).textContent = step[index]; });
});
