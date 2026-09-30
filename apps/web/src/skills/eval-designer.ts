export type EvalDesignKind = 'skill_cost' | 'skill_effect' | 'skill_compliance' | 'tool_relevance' | 'error_pattern' | 'custom';
export type EvalDesignDraft = {kind:EvalDesignKind;question:string;target:string;population:string;decision:string};
export const evalDesignKinds:Array<{value:EvalDesignKind;label:string;hint:string}>=[
  {value:'skill_cost',label:'Skill 加载成本',hint:'耗时、Token 与长尾分布'},
  {value:'skill_effect',label:'Skill 效果对比',hint:'有 Skill / 无 Skill 的观察性或受控比较'},
  {value:'skill_compliance',label:'Skill 指令遵循',hint:'模型是否遵守当时可见的 Skill 要求'},
  {value:'tool_relevance',label:'工具调用合理性',hint:'工具、参数与调用时机是否符合任务需要'},
  {value:'error_pattern',label:'错误模式',hint:'重复调用、共同报错与异常链路'},
  {value:'custom',label:'自定义问题',hint:'设计其他可由 Trace 证据回答的评测'},
];
export const defaultEvalDraft:EvalDesignDraft={kind:'skill_cost',question:'观察 Agent 加载目标 Skill 时，读取文档是否消耗了异常多的时间和 Token。',target:'待填写 Skill 名称及 revision',population:'待填写项目、Trace revision、模型、环境和时间范围',decision:'判断是否需要优化 Skill 内容或加载方式'};
export function buildEvalDesignerPrompt(value:EvalDesignDraft):string{
  const kind=evalDesignKinds.find(item=>item.value===value.kind)?.label||'自定义问题';
  const field=(text:string)=>text.trim()||'尚未确定，请先与我澄清';
  return ['请使用 $trace-eval-designer 与我一起设计一项 Trace 评测。','',`评测类型：${kind}`,`核心问题：${field(value.question)}`,`目标对象：${field(value.target)}`,`数据范围：${field(value.population)}`,`结果用途：${field(value.decision)}`,'','请先检查当前 Trace Hunter 能力和我提供的资料，只询问会改变评测结论的问题。','然后输出 Status: Draft 的 Eval Spec，包含分析单位、证据契约、查询计划、判定或指标、unknown 语义、校准案例、能力缺口和待决项。','涉及 Agent 当时的判断时必须使用 model_context，禁止信息穿越；历史 Cohort 差异不得直接解释为因果关系。','本轮只设计评测，不执行批量分析、不注册插件，也不创建 Harbor Task。'].join('\n');
}
