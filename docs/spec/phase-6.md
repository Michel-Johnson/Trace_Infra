# 第 6 阶段：ClickHouse、HTTP、跨节点对象存储

轨迹事件仍先写对象存储（S3 key）。ClickHouse 表是派生检索层，列名见 `docs/spec/clickhouse.sql`。本仓库保持标准库，进程内用 sqlite，文件在 `{objects}/clickhouse.sqlite`。Postgres 形状的索引仍在 `{objects}/index.sqlite`，不管快照本体。

控制面是标准库 HTTP。`/v1/*` 是第 0 阶段四条接口。`/sandboxes` 是 E2B 形状的别名：创建、snapshot/pause、fork。Restore 一定返回新的 `sandbox_id`，不用同一个 id 原地回滚。

跨节点：快照和索引放在共享 `objects` 目录（生产里是 S3 + Postgres）。每个节点自己的 `live/` 只放活 jail。节点 B 打开同一 `objects`、空的 live 根，用 `state_id` 做 `RestoreState`，得到自己的 `sandbox_id`。

进程内接口测试走 `backend=episode`。

## HTTP

```
POST /v1/runs
POST /v1/sandboxes/{id}/act
POST /v1/sandboxes/{id}/commit
POST /v1/states/{id}/restore
POST /v1/sandboxes/{id}/replay
POST /v1/sandboxes/{id}/branch
GET  /v1/states/{id}
GET  /v1/spans/{id}
GET  /v1/analytics/events?run_id=&span_id=&action=

POST /sandboxes                    task_id 创建，或 state_id 恢复（新 sandbox_id）
POST /sandboxes/{id}/pause         CommitState
POST /sandboxes/{id}/snapshots     CommitState
POST /sandboxes/{id}/fork          Branch，body.count 即 n
```

部分 Branch 失败时 HTTP 207，body 仍是 `children` 数组。

## 验收对照

按 `run_id` 能从分析表读出完整 observation，不是截断摘要。另一份 Runtime 只共享 objects、不共享 live 进程，仍能 RestoreState。HTTP 能走完 start → act → commit → restore → branch。
