# ATIF v1.8 Context 轨迹

当前 Adapter 实现版本：`1.0.0`。

只在根 `schema_version` 精确为 `ATIF-v1.8` 且 `agent`、顺序 `steps` 满足该版本签名时使用。不要把 v1.8 作为 v1.7 的字段别名。

- 缺少 `llm_call_count` 表示请求数未知；即使存在逐步 metrics，也只能映射为 `model_batch(request_count=null)`。
- `extra.context_management={type:compaction,boundary:replace}` 映射为 compaction event。没有真实 Context ID 时，`before`/`after` 保持 null。
- `subagent_trajectory_ref.trajectory_path` 只证明存在外部引用。未提供引用文件字节和 SHA-256 时，登记为 `missing` source；不得读取相邻文件、递归生成对象或分配 token。
- `bash_command` 是 Harbor Terminus 的 shell 工具名，保留原名并映射 `operation=bash`；只有带匹配 `source_call_id` 的 observation 才生成 execution Span。
- prompt/completion token 总数进入 v2 usage。token IDs、logprobs 和逐步 cost 以带来源指针的 `atif.metric_evidence` 保存；最终总量与逐步合计不同只报告范围差异，不归因给未提供的子轨迹。
- `timestamp`、请求 Context、状态和执行结果缺失时保留 unknown/missing；`duration` 工具参数是计划等待秒数，不是执行耗时。
