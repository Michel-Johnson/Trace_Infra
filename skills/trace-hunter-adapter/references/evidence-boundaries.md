# 证据与语义边界

- 一次真实执行生成一个 execution Span。读取文档、调用工具或模型请求只有在来源将其记录为独立执行时才是独立 Span。
- Tool proposal 与 execution 分开；模型输出了调用意图，不等于工具已执行。
- 每次 retry 单独成 Span，仅在有 attempt/retry 证据时建立 `retry_of`。
- 厂商不可拆分的聚合调用保存为 `model_batch`，不伪造多次模型请求。
- 用户输入按 Turn 划分；Message 独立保存。携带历史的累计快照不是新的用户消息。
- 模型请求的真实可见输入写 Context；不能从完整聊天历史反推模型当时看到了全部内容。
- 大正文、图片和附件进入内容存储；Span 只保留引用、摘要、截断状态和可检索投影。
- Trace、Span、Message、Context、Annotation 使用稳定 ID；ID 组成必须写入映射契约。
- `null`、字段缺失、0、空串、unknown 含义不同。状态没有明确成功/失败证据时保持 unknown，并说明缺失了什么。
- source_refs 必须定位到不可变原件及具体记录/路径；只有 source_refs 可解析并核对哈希，才可声称结论可追溯。
