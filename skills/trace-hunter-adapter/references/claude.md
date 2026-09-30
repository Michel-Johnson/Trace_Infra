# Claude 来源接入

## 来源优先级

新采集优先复用 Langfuse `claude-observability-plugin`，并在需要精确计时时用 Claude Code 官方 OpenTelemetry 补充。原始 session JSONL 是内容和身份归档；插件、OTel 与 JSONL 只能用稳定的 session/request/tool-call ID 关联，不能按时间邻近或文本相似强行拼接。

推荐链路：`Claude Code → Langfuse → API 原始导出 → Claude Adapter → v2`。插件直接连接 Langfuse，不能仅改 URL 就假定能向 Trace Hunter 上报。历史会话若运行时没有采集，安装插件后不会自动拥有历史遥测；保留精确耗时 unknown。

## 接入检查

- 明确 session ID、Claude Code/插件/Langfuse/API 版本、分页结束状态和导出窗口。
- 保存 Langfuse 原始导出、对应 session JSONL、子 agent 与外置结果的来源 ID、摘要和缺失项；凭据不得写入报告或 Git。
- 导出 session 下全部 trace/observation，不能只取最后一条或第一页。
- 保留模型请求、实际工具执行、proposal、授权等待和 retry 的边界；流式片段与累计 usage 不增加请求数。
- input/cache/output/reasoning token 先核对包含关系再聚合；缺失不是零，累计快照不能直接求和。
- 多轮、续接、fork、压缩和子 agent 只按来源证据建关系。完整聊天历史不等于模型单次请求的 Context。

仓库已注册的 `claude-export` 仅用于其已定义的历史结构化实验文件。原生 Claude session JSONL 或 Langfuse 导出结构不匹配时，按新格式流程开发 Adapter，不得强行指定 `claude-export`。

官方资料需按目标版本核对：

- https://github.com/langfuse/claude-observability-plugin
- https://langfuse.com/integrations/developer-tools/claude-code
- https://langfuse.com/docs/api-and-data-platform/features/public-api
- https://code.claude.com/docs/en/monitoring-usage
