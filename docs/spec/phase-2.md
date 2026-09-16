# 第 2 阶段：索引和原始轨迹

Postgres 表落地；完整 span 事件仍按第 0 阶段的 S3 key 写对象。ClickHouse 不在本阶段。example 任务文件不改。本仓库保持标准库，进程内用 sqlite3，表名和列名与 `docs/spec/schema.sql` 相同。

## 分工

索引（sqlite / 生产 Postgres）：`runs`、`states`、`spans`、`span_index`。一行 state 只含 `state_id`、父节点、`snapshot_uri`、`prompt_uri`、budget、`t`。快照本体不进库。

对象（本地目录，key 与 S3 相同）：

```
snapshots/{state_id}/metadata.json
snapshots/{state_id}/episode.json
runs/{run_id}/prompts/{state_id}.json
runs/{run_id}/spans/{span_id}/events/{t:08d}.json
```

`span_index` 只存检索字段和 `event_uri`。完整 `observation` 只在事件对象里。

活 sandbox 仍只在编排进程内存里。进程重启后 `sandbox_id` 作废；`state_id` 还能 Restore。

## 读接口

`Runtime.get_state(state_id)`：索引行加上从 `prompt_uri` 读出的 prompt。没有 prompt 则为 null。

`Runtime.get_span(span_id)`：span 元数据，加上按 `t` 升序的事件。事件含完整 observation。

## 验收对照

一次实验能按 `state_id` 找回快照和 prompt。一条 span 能按顺序读出全部 action，不被 `obs_summary` 截断。新 `Runtime` 打开同一 root 时，索引和对象仍在，Restore 仍可用。
