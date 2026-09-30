# 通用轨迹契约试验

本目录对应 [RFC 0002](../../../docs/rfcs/0002-general-trajectory-contract.md)，版本 `trace-hunter/2.0-draft.1`。**它是离线设计原型，不是线上已支持的输入格式。** 当前旧 /api/import 会拒绝此版本；线上 1.1 Schema 保持原样。backend-002 新增的 TraceRevisions 核心可将此格式作为 experimental 文档持久化，保存原始字节并验证结构/引用；它不核验外部附件、不授予回放或训练资格。服务接入状态见[迭代记录](../../../docs/implementation/backend-iterations.md)。

当前工程决策与 agent 接入方式见 [轻量接入设计](../../../docs/architecture/agent-friendly-ingestion.md)。`scripts/trace_agent.py describe / prepare / check` 已接通现有 1.1、ATIF-v1.7 flat 文件，以及 Claude、豆包原始导出的直接 adapter；原始导出不经过 1.1 中间格式。adapter 保留 `source.json` 原始字节、来源指针和损失报告，agent 不必手工构造内部 JSON。

Claude 与豆包原始导出必须显式指定来源格式和平台身份：

```bash
.venv/bin/python scripts/trace_agent.py prepare raw-claude.json \
  --format claude-export --run-id RUN --query-id QUERY --env-id ENV \
  --output tmp/claude-v2-bundle
.venv/bin/python scripts/trace_agent.py prepare raw-doubao.json \
  --format doubao-export --run-id RUN --query-id QUERY --env-id ENV \
  --output tmp/doubao-v2-bundle
.venv/bin/python scripts/trace_agent.py check tmp/claude-v2-bundle --full
```

`trace.schema.json` 由 `scripts/build_trace_v2_draft.py` 生成。小核心是 document/run/capture/sources/segments/spans；其余数组按采集能力提供。模型、工具、等待、聚合记录分别使用条件约束；来源未知和正文缺失有明确状态。`model_batch` 表示无法拆分的聚合请求，request_count 未知时填 null。

中性导入修订：run.query_id/env_id 可省略或 null，不要求先注册 Case、标准环境或评分插件。单条轨迹的 turns[] 表示用户轮次；批量导入的 items[] 每项是一条独立轨迹，见[导入规则](../../../docs/architecture/neutral-trace-import.md)。这是尚未冻结的草案修改，线上 1.1 不变。

## 执行

在仓库根目录，复用锁定的 Python 环境，无需新增包：

```bash
.venv/bin/python scripts/build_trace_v2_draft.py
.venv/bin/python scripts/build_trace_v2_examples.py
.venv/bin/python scripts/validate_trace_v2.py examples/drafts/trace-v2/multiturn-resume.json
.venv/bin/python -m unittest discover -s tests -p 'test_trace_v2_draft.py' -v
```

四份 [合成样例](../../../examples/drafts/trace-v2) 分别覆盖最低可浏览数据、多轮并行与重试/压缩/恢复、聚合调用、检查点分支。全部数值是契约示意，不能作为模型性能结论。synthetic-source.json 是样例声明文件，其哈希只证明声明原件一致，不把合成事件包装成真实日志。

多轮样例应得到：2 轮用户输入、4 个可辨认模型请求、4 次工具执行；已记录 input=255 / output=57（包含失败 attempt 与压缩分项，缓存不重复相加）；clock-1 的工具并集为 700 ms，另有一次未知工具保留原顺序。恢复后的 clock-2 没有对齐证据，原型不计算跨域总墙钟时间。

## 已验证的语义

- ID、来源、上下文、消息、工具结果及阶段引用；父级、依赖、恢复与上下文变化的无环性。
- 用户消息只能建立一个实际 turn；历史被重复引用不会增加用户轮数。
- 同 agent / kind / invocation / attempt 不重复；retry_of 从新 attempt 指向旧 attempt，depends_on 从消费者指向依赖，invokes 从模型指向工具。
- 时间端点、时钟域、首响应含义；派生 duration 需一致，独立上报的 duration 用 source_reported / duration_scope 声明，差异保留并报告。工具并集按时钟域分开。
- total 与 components 互斥；缓存 / reasoning 子项、完整用量的主计数、父子消费归属。
- 截断 / 脱敏 / 未知内容状态；原始 value / extensions 不被误解释为协议引用。

来源 adapter 的 16 项测试覆盖：既有 6 份输入工具 ID/计时和 token 保真、工具提案与执行分离、未知工具、重复片段、copied context、包幂等、原文哈希/指针、错误定位、损坏清单及用途准入拒绝。上游两份 ATIF 样例的对照结果另存于 [验证报告](../../../docs/research/interop-validation-2026-09-10.json)。

## 尚未实现与不得宣称的能力

- OTel/OpenInference 与嵌套 ATIF adapter、跨来源请求匹配、完整多模态内容块、token ID / logprob / loss mask 的严格训练 Schema。
- 自动确认 completeness 声明、读取外部附件。新 CLI 只校验包内明确提供的 source.json 字节与指针，不访问外部 locator；单独的 draft validator 仍只检查结构与引用身份。
- RFC 8785 digest、源记录跨 revision 一致性、批次乱序暂存、源采集 seal 和成本计算。平台文档版本、请求幂等、历史分页和数据库存储由独立 TraceRevisions 管理，不等于这些采集语义已实现。
- 自动检测所有没有 parent 关联的重复聚合消费，或恢复不可见的请求/时钟。adapter 仍须证明统计归属。
- tool_calls[] 现已定义独立提案，tool.proposal_id 关联真实执行。flat ATIF 中同一个 call ID 出现多个结果而没有 attempt 时会报歧义错误；更复杂的重试/分片映射需原生 adapter 证据。

验证通过只表示上述有限约束成立，不等于轨迹完整、任务通过或可直接训练。冻结版本前按 RFC 的验收矩阵接入真实来源。
