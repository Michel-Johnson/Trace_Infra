# Agent benchmark envelope

`agent-benchmark/1` 接受单条 JSON 对象，封套必须包含非空字符串 `id`、`benchmark`、`query`、`source_id`、`source_url`、`license`，以及对象或数组 `trace`。来源没有自声明 Schema，调用时必须显式传 `--format agent-benchmark/1` 和平台分配的 `run_id`、`query_id`、`env_id`。

Adapter 只识别下列稳定内部签名：

- APB：`trace.messages` 与 `trace.tools`，保留消息、模型响应、工具提案和已匹配执行。
- CTB：`trace.files` 中唯一的 `mini-swe-agent-1` trajectory，保留消息、shell 返回和其他文件正文。内嵌 JSON 的 source ref 只能定位到不可变文件字符串，报告会标记该粒度限制。
- ROOTSE：`trace` 是步骤数组，每步带 `action`；步骤 observation 不会被伪分配给单个 action。
- TEL：`trace.spans[]` 只有 `id/raw`；按有序 opaque step 保存，不猜测模型或工具边界。
- TRAIL：递归 `child_spans` 的 OTel/OpenInference 结构；保留父子关系、输入输出、状态、ISO-8601 时间和整数语义 token 计数。

顶层 `query` 始终成为可检索的 user Message。未知内部签名、重复 ID、无效 token/时间结构或歧义的多个主 trajectory 必须失败，不得按相似字段兜底。
