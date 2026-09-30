# 轨迹聚合与覆盖

第 008 轮已实现并通过隔离环境验收，已随 0d0148e 核心发布上线。POST `/api/v1/projects/{project_id}/traces/aggregate` 需要 traces:read；只查询数据库投影，不创建分析任务或运行插件。

当前上线合同为 1.15.0，部署范围见[发布说明](../project/release-20260914.md)；上文轮次与早期合同号保留为实现沿革。

```json
{"filters": {}, "revisions": "latest", "group_by": "harness", "limit": 50}
```

过滤与轨迹查询共用语义。group_by 可选 query_id、env_id、harness、model、status、index_state；省略只返回总体。最多返回 50 组，按匹配版本数降序；响应同时给出 group_count、remaining_group_count 和 truncated，不能把返回的前 50 组当作全部。

总体和各组都包含 matched_revisions、index_complete、index_failed、unindexed、indexed_revision_count、记录种类计数与记录状态计数。只对完整索引计数，indexed_revision_count 是对应覆盖分母。没有匹配版本时为已知零；有版本但无完整索引时，记录指标为 null。混合覆盖显示已索引部分的已知值，不能外推未索引部分。

错误率 numerator 为 error 记录数，denominator 为 ok + error 记录数；unknown 与其他状态不计入该分母，零分母的比例为 null。basis 固定为 indexed_records_with_known_outcome，统计范围包含所有记录 kind，不等于去重后的工具执行错误率。

总体与分组来自单次 SQL 快照，避免并发更新造成同一响应内口径漂移。它不是持久 SelectionSnapshot，也不承诺下一次读取相同。watermark.kind=coverage 只表达已观测索引覆盖；latest_indexed_at 是最近索引时间，不是连续处理水位。采集完整性与模型 token、时长、成本另有口径，本接口不推断。
