# DeepDiveResult 合同

产物为 UTF-8 `deep-dive-results.jsonl`，每条固定 revision 的 Trace 占一行。每行是可独立解析的 JSON 对象；不得写 Markdown 标题、汇总段落或跨 Trace 比例。以下示意排版为便于阅读，实际文件须将每个对象写在单独一行；示意身份与判断不是实际评测结果。

```json
{
  "schema_version": "deep-dive/1",
  "spec_ref": {"id": "spec-1", "version": "1.0.0", "digest": "sha256:example", "status": "Draft"},
  "trace_ref": {"project_id": "p", "run_id": "r", "revision": 1},
  "dimensions": {
    "result_quality": {"verdict": "unknown", "evidence": [], "missing": ["验收产物"]},
    "execution_process": {"verdict": "unknown", "evidence": [], "missing": ["可见上下文"]},
    "tool_use": {"verdict": "unknown", "evidence": [], "missing": ["工具执行结果"]},
    "efficiency_stability": {"verdict": "unknown", "evidence": [], "missing": ["耗时记录"]}
  },
  "findings": [],
  "spec_feedback": []
}
```

## 字段约束

- `spec_ref` 固定上游 Eval Spec 的 id、version、digest 和 Draft/Approved 状态。Draft 下的 verdict 只用于规则校准，不得作为正式评分。
- 四个 `dimensions` 键必须齐全。`verdict` 只取 `pass`、`issue`、`unknown`、`not_applicable`；有明确目标不涉及该维度才用 `not_applicable`，缺所需证据则用 `unknown` 并列出 `missing`。判断依据只能来自 Spec 的规则。
- `pass`/`issue` 必须引用足以支持该判断的证据；`unknown` 必须说明缺口。不能为了填满四项而制造确定结论。
- `evidence` 中的每项使用 `span_id` 或 `object_id` 等稳定身份，并尽可能带已核实的 `source_ordinal`/`source_ref`；同一行的证据只能属于 `trace_ref` 指定的 Trace。不要复制整条 Trace 正文。
- 每项 `findings` 记录 `dimension`、`statement`、`first_deviation`（证据引用或 `null`）、`impact`（可证实的影响或 `null`）、`evidence`、`counter_evidence`、`classification`（`observed`/`inferred`/`hypothesis`）及 `confidence`。未证实的归因只能是 hypothesis；没有首次偏差证据时不能填造。
- 每项 `spec_feedback` 记录 `rule_ref`、`problem` 和相关 `evidence`，用于反馈规则歧义、证据不可得或缺少判定条件。它不是新规则，也不能在本次精读中改写 Spec。

同一次输出的所有记录须引用同一 Spec 版本；若部分 Trace 因索引或权限无法分析，仍写对应行并把受影响维度标为 `unknown`。Reporter 只读取这些记录和其他已验证结果生成面向人的报告，不在此文件内生成摘要。
