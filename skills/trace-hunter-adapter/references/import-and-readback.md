# 正式导入与回读

本流程接在 `prepare` + `check --full` 之后。使用公开 API，不直连数据库；导入、轮询和读取不得触发分析。

## 1. 确认目标

- 使用用户指定或当前任务已经确定的网关 URL、现有 project ID 和认证配置。
- 读取 `GET /api/openapi.json`、`GET /api/v1/adapter-import-capabilities` 和 `GET /api/v1/trace-formats`，确认服务支持来源格式、大小和 v2 profile。
- 用户已要求导入现有项目时，转换、幂等提交、轮询和回读属于同一 workflow，不逐步重复确认。
- 唯一的既有 run/query/env 绑定可直接沿用并记录来源。目标项目或身份存在多个实质不同的候选，且无法从来源或公开接口确定时才询问；不得发明身份。

## 2. 提交原始来源

原始文件统一用 CLI upload。显式指定已注册格式时只运行 Adapter，不启动 Agent；标准 v2 产物仍用 import 入库。

```bash
python3 scripts/trace_hunter_cli.py --url "$TRACE_HUNTER_URL" --project "$PROJECT_ID" upload "$SOURCE_FILE" \
  --source-format "$SOURCE_FORMAT" --idempotency-key "$IMPORT_KEY" \
  --run-id "$RUN_ID" --query-id "$QUERY_ID" --env-id "$ENV_ID" \
  --expected-previous "$PREVIOUS" --wait
```

只传 capability 要求的绑定参数。PREVIOUS 来自准确 revision 历史；同一来源、格式、绑定和目标版本使用稳定幂等键。CLI 自动分块、续传并校验 SHA-256；完整文件上限见 max_resumable_source_bytes。批量传 --batch-id、--batch-total 与 --source-name。

## 3. 等待完成

上传完成响应的 job_id 即任务 ID，轮询：

```text
GET /api/v1/projects/{project_id}/tasks/{task_id}
GET /api/v1/projects/{project_id}/import-batches/{batch_id}
```

直到 job 为 `succeeded` / `failed`，或 batch 为 `completed`。失败时报告失败阶段、稳定错误码和 Adapter issues；不要换随机身份或幂等键绕过冲突。任务及批次进度持久化；重启可能令未完成任务失败，应核对任务错误与 revision 事实。自动导入的恢复/取消统一使用 tasks/{task_id}/retry 和 tasks/{task_id}/cancel；历史任务没有完整转换报告时不能补造。

## 4. 回读验收

成功至少验证：

1. task.result 的 run_id/revision 与 index_state=complete；新 Adapter 任务的完整报告及原件引用位于 task.result.import_result。
2. `GET .../traces/{run_id}/revisions` 包含目标 revision。
3. `GET .../traces/{run_id}/revisions/{revision}/content` 与本地转换后的 `trace.json` 字节摘要一致。
4. `POST .../traces/query` 的 latest 结果包含相同 run、revision、content digest 与 `index_state=complete`。
5. Agent 审阅来源格式的字段映射，再从不同变体、长度和状态抽 10 条 Trace（不足 10 条则全查），用 `objects/query`、`spans/window` 和 `search` 对照原文、v2 与入库投影，确认状态、Skill/action、metadata、正文所属字段、附件引用与截断状态一致。任一失败须修复 Adapter 并重新审核。
6. `GET .../observability` 显示 PostgreSQL、`pg_trgm`/trigram 索引正常，且没有相关投影失败。
7. 使用相同请求重试仍返回同一 job/revision，没有产生额外 revision。

语义探针回读属于导入验收；大范围分析和业务结论仍是后续任务。最终报告导入数量、失败数量、revision、幂等结果、索引状态、Agent 的字段映射审阅、10 条抽样结果及仍然 unknown 的信息。Agent 无法确认零损失时不得标记 Adapter 验收通过。
