# 执行模型

## 固定总体

读取目标服务 OpenAPI 与 capabilities，确认授权、查询字段、scope、索引状态和证据导出能力。用 `traces/query` 完整分页形成选集，并保存每个 `(project_id, run_id, revision)`。使用 `latest` 仅可发现候选；后续查询必须固定相同 revision 或等价不可变快照。无法固定则 `unsupported`，不产生正式指标。

记录原始候选数、Spec 排除数、选中数、已处理数、未处理数和各类缺证据数。若是随机/分层抽样，保存抽样框、种子、层、各层选中数；100 条样本仅支持这 100 条的结论，除非 Spec 明确定义可外推的抽样设计。分页沿用服务端 snapshot 和 next_cursor，不换过滤条件复用旧 cursor。

## 查询路线

```text
Approved Spec → traces/query 固定选集
  → objects/query 读取结构化对象
  → evidence-exports 创建任务、完成后下载完整批量证据（按需）
  → search 召回线索 / spans/window 复核争议（按需）
  → 每单位独立 rule/Judge 判定
  → metrics/query 核对已采集数值
  → case-results.jsonl + analysis-result.json
```

导出任务未完成或内容不可读时不能当作空证据。对象查询和导出应匹配同一固定选集、scope 与授权；无法按显式 revision 绑定时拆成逐 revision 查询或停止。服务上限、时间预算或数据截断必须进入 `partial`，未处理对象不能算未命中。只判断 Agent 当时可见信息时用 `model_context`；不回退到 `analysis`。

## 判定与质量控制

代码先处理确定的身份、关系、字段与缺失，再把 Spec 允许的固定证据交给 Judge。Judge 每个分析单位独立输入，输出四方面所需 verdict、证据身份和 unknown 原因；越界引用、解析失败或证据不支持的结论按失败/unknown 处理，不悄悄换规则重试。

`metrics/query` 只核对已采集的计数、耗时和用量；必须先验证其过滤范围和 revision 与固定选集完全一致，不一致时从固定证据计算，不能引用较大总体的统计值。语义评分从逐单位判定聚合，不能从 metrics 的 error_rate 直接推出任务失败率。系统性误判、Spec 歧义或校准反例触发停止并返回 Eval Designer；执行故障、数据缺口和真实被测行为分开记录。
