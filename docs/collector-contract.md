# 后续采集技能的共同契约

每个流程复用同一个 Trace 协议。新增 harness 时增加 adapter；新增业务时增加验收标准；不用为每个流程复制报告代码。

一个采集技能应交付：

1. 原始文件副本与 hash；按运行环境的合法入口采集。
2. 明确提供 run.query_id、run.env_id、run.id，环境的 isolation 与 network_access；版本 / 观测时间 / 快照缺失时保留未知。
3. 一份通过 `trace-v1.schema.json` 及引用校验的标准 JSON。
4. 来源 ID、phase、agent、request 和 tool 的稳定关联。
5. 明确的覆盖声明，缺失值为 null。
6. 可观察的产物与验收记录。最终回复写着「完成」不等于验收通过。

技能负责采集与格式转换；不要生成累计 token、瓶颈结论或总分。对于共享请求、并行工具、压缩和重试，记录身份和事实，由平台统一处理。

已有转换入口：

Doubao Work 可直接使用[本地采集命令](local-doubao-collector.md)，同时得到原始副本、来源清单和标准 `run.trace.json`，不依赖后台权限。

```bash
python3 scripts/normalize_trace.py path/to/execution-trace.json \
  --format claude-export --output run.trace.json \
  --query-id supermarket-base --env-id claude-env-v1
python3 scripts/normalize_trace.py path/to/doubao.trajectory.json \
  --format doubao-export --output run.trace.json \
  --query-id supermarket-base --env-id doubao-env-v1
```

`case_id/raw_turn/calls` 形式的豆包单轮评测导出使用 v2 离线 Adapter：

```bash
python3 scripts/trace_agent.py prepare path/to/raw.json \
  --format doubao-turn-export --output path/to/bundle \
  --run-id RUN --query-id QUERY --env-id ENV
python3 scripts/trace_agent.py check path/to/bundle
```

生成包中的 `source.json` 保留原始字节，`trace.json` 可直接导入平台。该来源只有工具执行时，不会伪造模型 Span、proposal、retry、父子关系或模型 Context。

今后增加适配器时必须带一个最小输入样本和回归断言：多少独立请求、多少工具、是否包含导出阶段、哪些字段缺失。原始格式的聚合数字只能作为核对材料，不能复制进标准协议后让平台照单全收。

同一 query_id 的运行可以进入同一对比组。env_id 标识环境配置；沙箱不自动代表禁止联网，非沙箱与联网环境需尽量记录当次时间和外部依赖。采集技能仅导入，不自动触发平台分析。

## 网络记录的边界

网络捕获完整性和语义轨迹完整性必须分别声明。原始字节与单调时钟先归档，SSE / JSON 解码后再映射 span；一个网络块不是一次工具或模型调用。记录失败、队列满、额度截断与异常断开都应进入采集 manifest，不能用已保存内容的 hash 证明未保存部分不存在。

网络观测时间保留为传输时间，不能覆盖后台模型或工具起止。查询后台原始响应时保留来源 ID、完整响应与 hash；不同来源有冲突时保留冲突，不静默拼接。已有[Doubao 导出与网关研究](research/doubao-export-and-capture.md)及[本机合成验证探针](../scripts/probe_wire_capture.py)。
