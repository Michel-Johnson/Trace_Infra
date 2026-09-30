# 批量产物合同

同一次运行产生 UTF-8 `case-results.jsonl` 和 `analysis-result.json`。前者每个 Spec 定义的分析单位一行，后者仅汇总这些逐单位判定及已采集指标；均不是面向人的报告。以下字段为结构示意，不是真实评测结果。

`case-results.jsonl` 每行：

```json
{"schema_version":"trace-hunter/batch-case/1","unit_ref":{"project_id":"p","run_id":"r","revision":1},"spec_ref":{"id":"spec-1","version":"1.0.0","digest":"sha256:example"},"dimensions":{"result_quality":{"verdict":"unknown","evidence":[],"missing":["验收证据"]},"execution_process":{"verdict":"unknown","evidence":[],"missing":["历史 Skill 内容"]},"tool_use":{"verdict":"not_applicable","evidence":[],"missing":[]},"efficiency_stability":{"verdict":"unknown","evidence":[],"missing":["耗时"]}},"findings":[]}
```

`analysis-result.json`：

```json
{
  "schema_version":"trace-hunter/analysis-result/2",
  "status":"partial",
  "spec_ref":{"id":"spec-1","version":"1.0.0","digest":"sha256:example"},
  "population":{"project_id":"p","selection":"明确的过滤与抽样方法","candidate_count":100,"units_selected":100,"unit_refs":[],"selection_digest":"sha256:example"},
  "processing":{"units_completed":0,"units_unprocessed":100,"units_excluded":0,"truncated":false},
  "dimensions":{
    "result_quality":{"pass":0,"issue":0,"unknown":0,"not_applicable":0,"unprocessed":100,"applicable_denominator":0,"coverage":0},
    "execution_process":{"pass":0,"issue":0,"unknown":0,"not_applicable":0,"unprocessed":100,"applicable_denominator":0,"coverage":0},
    "tool_use":{"pass":0,"issue":0,"unknown":0,"not_applicable":0,"unprocessed":100,"applicable_denominator":0,"coverage":0},
    "efficiency_stability":{"pass":0,"issue":0,"unknown":0,"not_applicable":0,"unprocessed":100,"applicable_denominator":0,"coverage":0}
  },
  "metrics":[],"patterns":[],"cohorts":[],"exemplars":[],"issues":[],
  "artifacts":{"case_results":"case-results.jsonl","case_results_digest":"sha256:example"},
  "method":{"query_digest":"sha256:example","judge":null}
}
```

## 统计约束

- `unit_refs` 必须列出实际选中的全部固定 revision 身份，示意空数组不能用于真实结果。`units_selected = units_completed + units_unprocessed = unit_refs` 的条数；排除项在选集形成前按 Spec 记录，不能在已选后为了提高通过率剔除。若分析单位不是 Trace，`unit_ref` 必须含对应稳定对象身份，且同一单位只出现一次。
- 每方面的 `pass + issue + unknown + not_applicable = units_completed`；`applicable_denominator = pass + issue`。`coverage = applicable_denominator / (units_selected - not_applicable)`，分母为 0 时用 `null`；如未处理项的适用性未知，这一口径是保守下界，必须注明。
- 通过率只在已完成、适用且证据充分的单位上计算，同时展示全部选集的覆盖率。`unknown` 不进通过率分母，但必须展示；未处理不得并入 unknown 或 pass。
- `metrics[]` 只放 Spec 已批准的数值指标，逐项记录值、单位、分子/分母、已知/缺失数、分组、计算方法和来源；耗时/Token 等直接统计与 Judge verdict 分开。不能平均各组 p95 得总体 p95。
- `patterns[]` 是重复问题及数量，`cohorts[]` 写各组人数、可判定数和剩余混杂，`exemplars[]` 引用代表性正例、反例与边界例的 `unit_ref` 和稳定对象证据；不贴整条 Trace。不能从少量 exemplars 推出总体频率。
- `issues[]` 区分 Spec 缺陷、数据缺失、能力/权限缺失、执行故障与被测行为。`partial`/`unsupported`/`insufficient_data`/`failed` 不得伪装成完整正式通过率；保留可核验的已处理事实。
- 结果只可与相同 Spec 版本、总体、单位、revision 策略和指标语义比较；重导入、回填证据或改阈值产生新结果。
