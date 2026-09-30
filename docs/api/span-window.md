# Span 邻域查询

`POST /api/v1/projects/{project_id}/spans/window` 以固定 `(run_id, revision, span_id)` 为锚点，按 `source_ordinal` 返回前后连续 Span。`before`、`after` 各为 0～100；锚点包含在 `spans` 中。

```json
{
  "anchor": {"run_id": "run-1", "revision": 1, "span_id": "skill-7"},
  "before": 50,
  "after": 20,
  "include": ["documents", "related_objects", "edges"],
  "preview_chars": 512
}
```

`documents` 是与窗口 Span 直接关联或经 Context 关联的正文投影，包含字段、受限预览、`text_state` 和来源引用；不返回不可变原件全文。`related_objects` 返回 Message、Context、ToolCall 等对象身份，`edges` 返回已记录关系。附件超过服务上限时 `attachments_truncated=true`。

`ordering.basis=source_ordinal` 只证明来源数组顺序，不证明因果、同一并行分支或依赖。`has_more_before/after` 表示窗口外仍有 Span，可用边界 Span 继续读取。

接口只读、不触发分析或索引重建，要求 `traces:read`。它不开放给 evaluator-only 入口；模型当时真正可见的信息仍使用 `model_context` 查询。
