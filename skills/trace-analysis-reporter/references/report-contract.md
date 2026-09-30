# 报告合同

## 输入

- Eval Spec：身份、版本、状态、问题、总体、方法和指标语义。
- Analysis Result：Spec digest、固定 revision、覆盖率、metrics、findings、evidence 和 issues。
- DeepDiveResult JSONL：逐条 Trace 的四方面判定、证据、缺口、发现及 Spec 反馈；Draft Spec 下仅作校准材料，不得当正式评分。
- 可选业务背景：读者、决策和发布范围，不得改变已有结果。

缺少 Spec 或 Result 身份时，可以生成“探索报告”，但不能标为正式评测报告。

## 最小报告结构

1. 标题、状态、生成时间和输入版本。
2. 执行摘要：最重要的 3–5 个结论与决策含义。
3. 范围与方法：总体、分析单位、revision、排除、模板和 Judge。
4. 数据覆盖：evaluated、unknown、excluded、truncated 及字段覆盖率。
5. 关键结果：指标、分母、差异、方向和证据。
6. 典型案例：代表性正例、反例、边界和未知。
7. 限制与替代解释。
8. 建议与后续验证。
9. 证据索引和机器可读附件。

## 状态

- `exploratory`：包含未全量验证假设。
- `evaluated`：来自 Approved Spec 的完整或明确 partial 运行。
- `superseded`：已有新版本结果，不应继续用于当前决策。
