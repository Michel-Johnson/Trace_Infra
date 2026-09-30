# 抽样与证据

## 抽样目标

抽样用于扩大问题发现面，不用于估计全量比例。优先覆盖：明确成功、明确失败、长尾耗时、数据缺失、不同模型/环境/Skill revision，以及用户特别关心的行为。

不要只抽搜索命中的正例。至少加入未命中或结果相反的对照样本，否则无法识别查询偏差与替代解释。

## 样本清单

在分析前固定：项目、run_id、revision、选中原因、任务/模型/环境、已知覆盖状态。若样本由 literal、regex 或错误关键词召回，标明它只是候选来源。

## 事实账本

每条事实至少包含：

- 稳定身份：project、run、revision、object/span。
- 发生顺序及其依据：时间、source order 或显式 edge。
- 对象类型：Message、Skill、Tool proposal、Tool execution、Result、Error。
- 当时可见性：`model_context` 的 pass/fail/unknown。
- 原始状态、耗时、Token 与正文完整性。
- source_refs 或精确片段。

只有账本事实可以进入分析。摘要、标题和搜索分数不能替代原始证据。

## 置信度

- `high`：身份、关系、时序、可见性和关键正文均明确，并检查过反例。
- `medium`：核心事实明确，但关系或意图判断依赖有限推断。
- `low`：缺少关键 Context、关系或完整正文，只能形成待验证问题。
