# Infra 工作流

执行前读取目标服务的 OpenAPI 和 `capabilities`。优先使用 Core HTTP API；只有仓库实际提供 CLI 时才使用 CLI，并以其实际帮助为准。
使用 CLI 时先读 [最小合法查询与错误处理](../../trace-hunter-cli/references/cli.md)；不要猜测 `trace list`、`span --kind` 或把 `run_id` 写在请求顶层。命令失败先保留原退出码和错误 JSON，不经吞错管道判断成功。

1. 读取目标服务的 OpenAPI 与已公布的查询 capabilities，确认 `objects/query`、`metrics/query` 可用字段和 `model_context` 语义；不猜测已删除的评测路由。
2. `POST /api/v1/projects/{project_id}/traces/query` 固定 project、run 与 revision，检查 index/coverage 状态。
3. `POST /api/v1/projects/{project_id}/objects/query` 在固定快照中读取相关 Span、Message、Context、ToolCall 的身份、payload 与来源。按需要的字段取证；不能因只查到部分种类就称“完整时间线”。
4. 以关键 `span_id` 调用 `POST /api/v1/projects/{project_id}/spans/window` 查看前后步骤、正文预览和显式 edge。`source_ordinal` 只证明来源顺序，不证明因果；窗口两侧有更多记录时继续读取或标明边界。
5. 正文线索可用 `POST /api/v1/projects/{project_id}/search` 的 literal/regex 在已选 run/revision 内召回，再回查对象；片段截断或正文投影不足时，按权限读取现存的原件/对象详情，不把预览当全文。
6. 用 `POST /api/v1/projects/{project_id}/metrics/query` 核对已采集时长、用量和错误数，检查 known/unknown 覆盖；统计值不自动构成语义评分。
7. 判断 Agent 当时决策，只用 `model_context` 中确认对其可见的材料；缺失可见性时该项判断为 `unknown`。父子、invokes、retry 只按稳定身份及实际返回的显式关系判断，不按名称或时间自行 Join。

输出前按冻结 revision 回查每条确定结论的 `span_id`/对象 ID；写了 `source_ordinal` 时核对其精确值。找不到、序号不一致或引用不能支持判断时降为 `unknown`，并保留查询问题。完成提交或最终回答不自动等于任务成功；没有独立终态时只描述已观察动作。环境工具失败与 Agent 误用分开，只有确认替代工具或路径当时可用，才评价其未恢复行为。

所有分页必须读取完整，或在报告中明确当前页边界。超过服务端对象或关系限制时缩小总体，不在客户端静默截断。HTTP 非 2xx、能力声明与响应不一致、或索引未完成时，把影响写入 `data_coverage` 而不是降级成无声失败。
