# Trace Hunter 能力地图

## 数据链路

```text
原始 Trace
  → Adapter 识别、转换、语义校验
  → 标准 v2 Trace
  → Import API 切分并写入 PostgreSQL
  → Trace / Object / Edge / Search Document 投影与 pg_trgm 索引
  → CLI 查询、窗口、正文检索与指标统计
  → Eval Spec → DeepDiveResult JSONL → Batch Analysis → Report
```

导入只建立事实和索引，不自动分析。分析结果独立版本化，不反写原始 Trace。

## 能力与入口

| 能力 | 主要入口 | 关键结果 |
|---|---|---|
| 来源接入 | Adapter Skill、`upload --source-format FORMAT` | v2、语义损失、导入与回读状态 |
| 结构化查询 | `trace`、`span`、`object-query` | Trace/Span、Skill、Tool、状态、绝对时间、attributes、模型、环境 |
| 正文搜索 | `search` literal/regex | 命中片段、范围、截断状态、稳定身份；Trigram 仅是索引实现 |
| 邻域 | `span-window` | 前后 Span 及已有关系 |
| 批量取数 | `evidence-export` | 固定快照、NDJSON 证据、完整分页与覆盖边界 |
| 任务可视化 | `task create/update/watch` | 导入、评测与长分析的实时进度和终态 |
| 分析链路 | Eval Designer、Deep Dive、Batch Analyzer、Reporter | Eval Spec、逐条 DeepDiveResult、批量 Analysis Result、报告 |

## 选择原则

1. 输入格式未知或映射异常，先处理 Adapter，不能直接导入。
2. 已有平台数据先使用 CLI 发现真实字段和值，再选择分析方法。
3. 正式评测先由 Eval Designer 设计 Spec，Deep Dive 按 Spec 精读少量 Trace 校准规则；批准后才批量执行。
4. 用户只要查询结果时不要自动启动评测；用户只要报告时不要重新扫描数据。
5. 每步都保留 revision、分页、覆盖率、`unknown`、截断和来源证据。

命令、字段和限制应继续读取当前 `trace-hunter-cli`、`trace-hunter-adapter` 与分析 Skill，避免本地图成为第二套易漂移合同。
