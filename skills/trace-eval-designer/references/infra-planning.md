# Infra 查询规划

本参考用于把 Eval Spec 映射到 Trace Hunter 当前能力。命令只是能力示意；执行前必须以目标服务的 `capabilities` 和仓库内 CLI 帮助为准。

## 先确认能力

从 Trace Hunter checkout 运行：

```bash
.venv/bin/python scripts/trace_hunter_cli.py capabilities
```

不存在 `.venv` 时再使用 `python3`。不要假定所有部署都支持相同端点，也不要直接查询数据库绕过证据和权限边界。
执行 CLI 查询前读 [最小合法查询与错误处理](../../trace-hunter-cli/references/cli.md)；先按契约运行小页，再扩展查询，不猜测命令、flag 或 JSON 顶层字段。HTTP 422 和非零退出码必须保留为失败，不用 `2>&1 | head` 掩盖。

## 需求到能力的映射

| 评测需要 | 首选能力 | 设计注意事项 |
|---|---|---|
| 固定 Trace 总体、版本和环境 | `trace query` | 最终 Spec 固定 revision；分页必须完整 |
| 按模型等维度统计 Trace | `trace aggregate` | 先定义分母和缺失策略 |
| Skill、Tool、状态、耗时和父子结构 | `span` | 缺失 duration/status 保持未知 |
| 已知文本、错误签名或命令 | `search literal/regex` | 先结构化缩小范围；命中不是行为判定 |
| 同一 Skill 的耗时分布 | `metrics-query` | 报告样本量及缺失耗时数量 |
| 对象与正文证据 | `object-query`、`evidence-export` | 明确版本、分页、payload 与截断边界 |
| 包含/不包含某行为的 Trace 集 | 固定样本上由评测器逐条判定 | 有/无行为不自动构成可比 Cohort |
| A→B→C 行为链与中断 | `span-window` 与对象证据 | 来源顺序不自动等于因果关系 |
| 查看完整原件 | `trace content` | 仅在片段不足时读取，并遵守权限范围 |

结构化查询和指标由服务端执行，语义判断由评测器完成，避免下载海量 Trace 后在客户端按名称拼接。

## 两种证据范围

### `analysis`

用于事后诊断，可以在授权范围内跨对象和跨 Trace 检索。它可以回答“数据中出现了什么”，不能证明“模型当时知道什么”。

### `model_context`

用于判断 Agent 某一时刻的选择是否符合它当时可见的信息。必须提供准确 `visible_to`；返回 `fail` 或 `unknown` 时不得降级到 `analysis` 后继续给出行为合规结论。

## 查询计划写法

每一步都写出输入集合、操作、连接键、产出和失败语义。例如：

```yaml
query_plan:
  - id: candidate_runs
    operation: trace.query
    input: fixed project and revisions
    output: run identities
  - id: skill_invocations
    operation: span.query
    input: candidate_runs
    filters:
      skill_name: lark-cli
      skill_action: load
    join_on: [project_id, run_id, revision]
  - id: later_tools
    operation: object.query
    input: skill_invocations
    verification: evaluator checks recorded source order
    constraints:
      same_trace: true
      after_source_order: true
    output: tool spans with evidence
```

不得只写“搜索相关 Trace”。计划应让实现者知道候选如何收窄、对象如何连接、时序如何验证、未知如何产生。

## 能力缺口表达

区分三种结果：

- `supported`：现有字段和接口可直接得到所需证据。
- `partially_supported`：可以召回候选，但仍需人工、Judge 或新增连接/聚合步骤。
- `unsupported`：关键证据未采集或接口无法表达，不能可靠评测。

每个缺口写成“需要什么 → 当前缺什么 → 错判风险 → 最小补齐能力”。不要把建议新增索引写成已经支持，也不要用客户端全量扫描掩盖服务端能力缺失。

## 安全与可复现

- 评测 Agent 使用 evaluator-only 入口；若还能访问原件或 `analysis` 范围，应停止而不是继续。
- 凭据只通过既有安全配置提供，不写入 Spec、命令示例或报告。
- 记录项目、revision、过滤器、分页状态、模板版本和查询能力版本。
- Trigram 是正文候选召回索引，不负责 Trace 隔离、Span 关系、时间顺序或最终判定。
