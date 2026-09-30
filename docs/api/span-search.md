# Span 查询与正文搜索

状态：接口合同以运行中 `/api/v1/trace-search-capabilities` 为准；本章说明查询与证据边界。

该能力只查询从不可变 Trace revision 生成的 `trace-index/4` 投影，不跟随外部附件或来源 locator，也不会触发分析或隐式重建。结构化 Span 查询不读取正文；正文搜索只读独立的可重建搜索文档。一次真实工具或模型执行对应一个 Span；只有 proposal、没有结果或执行时序的工具提议不会被当成已执行 Span。

## 接口

- `GET /api/v1/span-query-capabilities`：返回可选字段、过滤器、排序和页大小限制。
- `POST /api/v1/projects/{project_id}/spans/query`：按 skill、action、kind、status、父子关系、调用标识和时长范围精确过滤；支持来源顺序或耗时倒序，以及 latest/all revision。

查询中的 `duration_ms` 优先使用同一时钟区间的 `end_ms-start_ms`，缺少完整区间时回退到来源记录的 duration。`interval_duration_ms` 与 `source_reported_duration_ms` 分开返回，未知值保持 null，不补零。

## 历史数据回填

迁移后显式执行：

```bash
DATABASE_URL='postgresql+psycopg://…' .venv/bin/python scripts/rebuild_span_indexes.py
```

脚本只重建缺失或失败的当前版本投影，支持 `--after-project`、`--after-run`、`--after-revision` 断点续跑；重复执行幂等。线上发布脚本会在历史 revision 回填后自动执行它。

## MVP 边界

`POST /api/v1/projects/{project_id}/search` 支持 literal 与受限 regex。PostgreSQL 使用 `pg_trgm` GIN 索引；SQLite 仅为测试/本地回退扫描。查询必须显式选择 `analysis` 或 `model_context` scope：前者除 `traces:read` 外还要求 `traces:search:analysis`；后者要求精确 run/revision/model_span，只有可见性为 pass 才搜索该 Context 的 request 与 messages，fail/unknown 均返回空结果。评测身份只授予 `traces:read`，不能切换到全局分析搜索。可见性边界仍在 model_context 查询中执行。

搜索结果返回最多 512 个 Unicode 字符的 `snippet`。literal/regex 同时返回相对片段的 `[start,end)` `match_ranges`；`snippet_truncated` 表示只返回正文局部，`text_state=truncated` 表示采集投影本身已经截断，两者不能混淆。

正则正式方言为受限 PostgreSQL ARE（见 `GET /api/v1/trace-search-capabilities` 的 `regex_dialect`、`regex_examples`、`regex_max_chars`）。默认 `regex_syntax=postgresql_are`；词边界写 `\y`，`\b` 在 ARE 中是退格符。显式 `regex_syntax=portable` 时，仅把字符类外的 `\b`/`\B` 转成 `\y`/`\Y`，响应 `effective_pattern` 给出最终表达式。服务端用 PostgreSQL 对模式校验、过滤和片段位置计算，避免 Python 与 PostgreSQL 方言混用；SQLite 回退仅用于本地测试。最长 256 字符，拒绝环视、反向引用及分组后紧接 `*`、`+`、`{`，超出安全预算明确失败。

`trace-hunter/2.0-draft.2` 在 content 上增加 `available_at`，并允许 Adapter 保存最长 100000 字符的 `search_text`；来源 ref 仍是事实依据。缺失时间、缺失完整 Context 或跨时钟不可比较时只能判为 unknown，不能默认安全。当前 Adapter 尚未采到完整模型 request，因此其真实 Context 通常仍是 unknown。

当前仍不支持跨 Span 序列模式、关键路径、自动根因归因或实时增量 Trace。搜索只索引 v2 内联 value/search_text，不跟随外部 locator。耗时分布使用 metrics/query；异常阈值由评测规范定义。
