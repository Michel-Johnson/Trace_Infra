# Eval Spec

Eval Spec 是一项 Trace 评测的控制面文档。它描述问题、证据、计算和判定，不复制 Trace 原文，不包含凭据，也不把查询结果写成预设结论。

## 推荐结构

```yaml
spec_id: skill-load-cost
version: 0.1.0
status: Draft # Draft | Approved | Deprecated

question: lark-cli Skill 的加载是否产生异常耗时或 Token？
decision: 用于判断是否需要优化 Skill 内容或加载方式

population:
  project_id: PROJECT
  revisions: explicit
  filters: {}
  exclusions: []

unit_of_analysis: skill_invocation
scope:
  mode: analysis # analysis | model_context
  visible_to: null

dimensions:
  - skill_name
  - skill_revision
  - model
  - environment

required_evidence:
  - field: span.duration_ms
    on_missing: unknown
  - field: span.input_tokens
    on_missing: unknown

method:
  kind: statistics # rule | statistics | llm_judge | hybrid | controlled_experiment
  query_plan: []
  calculations: []
  judge_rubric: null

metrics: []
verdict:
  values: [pass, fail, unknown]
  rules: []

calibration_cases:
  positive: []
  negative: []
  boundary: []
  insufficient: []

output:
  evidence_identity: [project_id, run_id, revision, span_id]
  required_fields: [sample_count, coverage, unknown_count, evidence]

capability_requirements: []
known_gaps: []
assumptions: []
open_decisions: []
```

允许使用 Markdown 编写，但必须保留这些语义。没有内容的字段可以省略，不能用模糊段落代替关键边界。

## 必须说明的设计

### 问题和决策

`question` 必须能由数据回答；`decision` 说明答案将影响什么行动。例如“Skill 好不好”不可直接执行，应拆成加载成本、任务结果、工具行为或合规性等独立问题。

### 总体与分析单位

明确纳入哪些项目、revision、时间、模型、环境和任务。分析单位可以是 Trace、Skill invocation、Span、Tool attempt、行为链或匹配后的任务对；分母必须与单位一致。

### 证据契约

每项证据包含来源字段、完整性要求、身份连接方式和缺失策略。正文片段不足以证明完整行为时，应要求读取完整记录或返回未知。

### 方法与判定

把候选召回、证据连接、计算、语义判断和最终判定分开。阈值必须说明来源；探索性阈值标为假设，不伪装成业务标准。
设计重试规则时，`retry_of` 或可复核的同任务证据才能把两次调用连成确定重试；仅同名、同参数或先后出现时输出 `retry_candidate`。失败后合理放弃、独立任务再次调用、修复前提后再调用，都应进入校准反例；缺任务上下文时不得自动把 `permanent_failure_count` 判成风险。

LLM Judge 的 rubric 至少定义：允许看到的输入、评价维度、输出 Schema、证据引用、`unknown` 条件和禁止使用的信息。不要让 Judge 自行扩大数据范围。

### 校准

至少准备明确正例、明确反例、边界例和证据不足例。评审重点是判定规则是否稳定，而不是措辞是否一致。若多个评审者可能合理分歧，应记录分歧维度或拆分指标。

## 评审清单

- 用户的实际决策是否清楚？
- 分析单位和分母是否一致？
- 查询范围是否固定且可复现？
- 总体断言是否有完整分页、分层分母、覆盖率和反例检查？局部样本是否被误写成全项目事实？
- Agent 当时可见信息与事后信息是否分离？
- 缺失数据是否会错误变成 0、否或失败？
- 搜索命中是否还经过结构、关系和时序验证？
- Cohort 是否控制任务、模型、环境和版本差异？
- 指标能否被某种无意义捷径刷高？
- 每个结论是否能回到稳定证据身份？
- 当前 Infra 不支持的步骤是否明确列出？
- `verdict.rules` 是否与 `known_gaps`、`assumptions`、`open_decisions` 一致？待决阈值或候选关联是否被误写成确定判定？

只有待决项已解决、校准案例经过评审、查询和判定边界均明确时，才将状态改为 `Approved`。任何影响总体、证据、判定或版本的修改都必须升版本并重新评审。
