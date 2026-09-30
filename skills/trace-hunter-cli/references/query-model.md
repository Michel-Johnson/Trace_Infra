# CLI 查询模型与证据边界

## 可组合事实

1. **Trace revision**：一次不可变导入版本，包含格式、来源摘要、索引状态和记录数量。
2. **Span**：一次真实执行或被来源明确声明的聚合执行。结构化字段包括 kind、name、operation、status、Skill、时间、父子关系及来源。
3. **搜索文档**：Span 输入/输出、Message、Context、ToolCall 参数等正文投影。它通过 `run_id`、`revision` 和可选 `span_id` 回到执行事实。
4. **关系与分析结果**：`trace_edges` 保存来源声明的关系；窗口可展开来源已记录的关系；历史分析产物不反写原件。
5. **查询投影**：`trace-index/4` 保存来源明确的绝对时间、Session 和 attributes；无法从可靠时钟计算时保持未知。

Span 邻域以不可变 revision 内的 `source_ordinal` 为骨架；Message、Context 和 ToolCall 通过 `span_id` 或关系边展开。它适合读取“某次执行前后发生了什么”，但顺序邻近不能替代父子、依赖或因果证据。

模型/环境与 Skill/Tool 可在对象查询中一起过滤或由 metrics-query 分组。需要按行为分组时，评测器在固定样本及完整证据上判定，缺失记录不能算未发生。Token 未记录时保持未知。

对象聚合的错误率只按状态明确的记录计算。读取 `status_coverage` 核对实际分母；`error_rate=null` 且 `issues.code=STATUS_DATA_MISSING` 表示原始数据没有可确认状态，不能解释为零错误。部分状态未知时，`STATUS_DATA_PARTIAL` 会说明未计入的数量。

`span_id` 相同才表示正文与该次执行直接关联。Message 或 Context 可能没有 Span；这时只能报告其对象身份，不能擅自归因耗时。

## 搜索模式

- `literal`：大小写敏感的原样包含；适合工具名、路径、ID 和稳定短语。
- `regex`：先由 trigram 缩小候选，再执行受限正则；返回精确片段与匹配区间。

返回字段：

- `snippet`：最多由服务能力声明长度的正文片段。
- `snippet_start`：片段在搜索文档中的 Unicode 字符偏移。
- `match_ranges`：相对 `snippet` 的 `[start,end)` 精确区间。
- `snippet_truncated`：片段不是完整搜索文档。
- `text_state`：来源投影本身是否完整；它与片段截断是两件事。

## 常用组合

### Skill 文本与耗时

1. `span --skill-name NAME --all-pages` 获取执行集合和耗时。
2. 将 Span ID 分批传给 `search --span-id ...`，搜索其 input/arguments/output。
3. 按 `(run_id,revision,span_id)` 连接结果；同名 Span 跨 Trace 不能直接合并身份。
4. 排序或计算分位数，并同时报告无耗时或无文本的样本。

### 错误正文定位

先用 Span 的 `status=error` 缩小范围，再在这些 Span 内做 literal/regex。不要先宽泛搜索整个语料后把所有命中当错误执行。

### 模型当时可见内容

使用 `model_context` scope，并在 `--body` 中提供完整 `visible_to`。可见性不是聊天历史推断；报告返回的 pass/fail/unknown。

## 不能自动推断

- 缺失 duration 不等于 0。
- Tool proposal 不等于工具已执行。
- 文本提到某个 Skill 不等于该 Span 调用了 Skill。
- `source_refs` 指向采集来源，不必然等于工具访问的业务资源。
- 当前页统计不等于全量统计。
- 相对 `start_ms` 不能用于跨 Trace 时间窗口；跨 Trace 查询必须使用有来源依据的 `start_at/end_at`。
- `next_source` 只表示来源顺序中的下一对象，不自动等于因果关系。
- `model_context` 的 `fail/unknown` 表示无法证明可见，不能降级为事后全量查询。
- Skill 指令不是安全边界；评测必须由 evaluator-only API 在权限层拒绝 analysis 和原件接口。
