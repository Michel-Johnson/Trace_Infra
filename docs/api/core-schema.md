# 核心数据库 Schema

迁移 `021_core_schema_consolidation.sql` 和 `022_core_schema_cutover.sql` 将历史数据库收敛为七张业务表和一张迁移记录表：

| 表 | 权威性 | 职责 |
|---|---|---|
| `projects` | 事实 | Trace 命名空间 |
| `traces` | 事实 | run 身份、最新 revision 和最新元数据 |
| `trace_revisions` | 事实 | 不可变原件引用、导入幂等、逐版本元数据和投影状态 |
| `trace_objects` | 投影 | Span、Message、Context、ToolCall；正文只留在搜索投影或原件 |
| `trace_edges` | 投影 | parent、retry、proposal、Context→Message 等关系 |
| `trace_search_documents` | 投影 | 可搜索文本与 PostgreSQL `pg_trgm` GIN 索引 |
| `analysis_results` | 派生 | 指定 revision、算法版本和 scope 的不可变分析结果；跨 Trace 结果以首个 resolved revision 为锚点，payload 保留完整 revision 集合和确定性 result ID |
| `schema_migrations` | 运维 | 已执行迁移及校验和 |

`trace_objects`、`trace_edges` 和 `trace_search_documents` 可以从 `trace_revisions` 指向的原件重建。迁移 `025` 在原表增加 JSONB 属性、绝对时间、Session 字段及属性/时间/Session/反向关系索引，不增加业务表。`trace-index/4` 从不可变原件重建这些字段，不创建新 Trace revision。

数据库不保存 Principal 或 Credential。生产入口必须限制 API 来源，并由反向代理完成用户认证；API 仅信任 `TRACE_HUNTER_OPERATOR_NETWORKS` 中的直接网络对端。
