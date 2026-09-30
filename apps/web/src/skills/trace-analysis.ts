export type AnalysisSkillKind='deep-dive'|'batch-analyzer'|'analysis-reporter';
export type AnalysisSkillDraft={goal:string;target:string;scope:string;output:string};
export type AnalysisSkillConfig={kind:AnalysisSkillKind;name:string;skill:string;description:string;goalLabel:string;targetLabel:string;scopeLabel:string;outputLabel:string;defaults:AnalysisSkillDraft};

export const analysisSkillConfigs:Record<AnalysisSkillKind,AnalysisSkillConfig>={
  'deep-dive':{kind:'deep-dive',name:'Trace Deep Dive',skill:'trace-deep-dive',description:'按 Eval Spec 精读小样本 Trace，产出逐条证据记录。',goalLabel:'Eval Spec',targetLabel:'样本选择',scopeLabel:'可见性与范围',outputLabel:'输出产物',defaults:{goal:'待填写 Draft/Approved Eval Spec 路径、ID、版本和 digest',target:'包含成功、失败、边界和数据缺失情况的固定 Trace revisions',scope:'判断 Agent 决策时只使用当时可见的 model_context',output:'输出 deep-dive-results.jsonl，每条 Trace 一行，包含四方面判定、证据和 Spec 反馈'}},
  'batch-analyzer':{kind:'batch-analyzer',name:'Trace Batch Analyzer',skill:'trace-batch-analyzer',description:'执行 Approved Eval Spec，对全量 Trace 做可追溯分析。',goalLabel:'执行目标',targetLabel:'Eval Spec',scopeLabel:'数据总体',outputLabel:'结果要求',defaults:{goal:'执行已批准评测，并对命中、未命中、边界和 unknown 做证据复核。',target:'待填写 Approved Eval Spec 路径、ID、版本和 digest',scope:'待填写项目、固定 revisions、过滤条件和运行预算',output:'输出版本化 Analysis Result，包含分母、覆盖率、unknown、issues 和证据'}},
  'analysis-reporter':{kind:'analysis-reporter',name:'Trace Analysis Reporter',skill:'trace-analysis-reporter',description:'把评测结果与精细分析整理成可审计报告。',goalLabel:'报告目标',targetLabel:'输入结果',scopeLabel:'读者与发布范围',outputLabel:'格式与重点',defaults:{goal:'把现有 Trace 分析结果整理成决策者可理解、审阅者可复查的报告。',target:'待填写 Eval Spec、Analysis Result 与 DeepDiveResult JSONL 的路径或版本身份',scope:'研发与评测人员；内部审阅；正文最小引用并脱敏',output:'输出 Markdown 或自包含 HTML，突出覆盖率、关键结果、案例、限制和建议'}},
};

export function buildAnalysisSkillPrompt(kind:AnalysisSkillKind,draft:AnalysisSkillDraft):string{
  const config=analysisSkillConfigs[kind],field=(value:string)=>value.trim()||'尚未确定，请先与我澄清';
  const guard=kind==='deep-dive'?'先确认 Draft/Approved Eval Spec；逐条输出 JSONL，不生成报告或总体比例。严格区分 observed、inferred、hypothesis，并避免信息穿越。':kind==='batch-analyzer'?'只执行 status=Approved 的固定版本 Eval Spec；完整分页，不把搜索命中直接当结论，缺失证据返回 unknown。':'不要重新执行评测或产生未经验证的新结论；保留分母、coverage、unknown、partial 和相关性边界。';
  return [`请使用 $${config.skill} 完成以下任务。`,'',`${config.goalLabel}：${field(draft.goal)}`,`${config.targetLabel}：${field(draft.target)}`,`${config.scopeLabel}：${field(draft.scope)}`,`${config.outputLabel}：${field(draft.output)}`,'',guard,'开始前先检查当前 Trace Hunter capabilities、输入版本和证据完整性；能力不足时明确报告缺口，不绕过 Infra API 或直接查询数据库。'].join('\n');
}
