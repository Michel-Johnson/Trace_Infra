# 统一任务进度

统一任务接口让网页、CLI 和 Agent 读取同一份后端状态。当前 Adapter 导入会自动注册任务；评测、分析和其他 Agent 工作流可显式创建任务并上报进度。

## 接口

- `GET /api/v1/task-capabilities`：状态协议、任务类型和同步方式。
- `POST /api/v1/projects/{project}/tasks`：以幂等 `request_key` 创建 Agent 任务。
- `GET /api/v1/projects/{project}/tasks`：任务中心列表，可按 `state`、`kind` 过滤，使用 `next_cursor` / `after` 稳定续页。
- `GET/PATCH /api/v1/projects/{project}/tasks/{task_id}`：读取或上报进度。
- `GET /api/v1/projects/{project}/tasks/{task_id}/events`：SSE 实时快照；`Last-Event-ID` 可断线续读。
- `POST /api/v1/projects/{project}/tasks/{task_id}/cancel`：取消 Agent 任务；运行中的导入不做不安全中断。

任务状态保存在内容目录旁的 `task-state/tasks.json`，不增加数据库表。服务重启后，已完成任务仍可读取；原来处于 queued/running 的 Agent 任务会明确变为 `failed / SERVICE_RESTARTED`，不会伪装成仍在运行。

Task 列表是有界运行历史，响应的 `retention_limit` 声明当前保留上限。大批导入的完整总量、成败计数和进度以 `/import-batches/{batch_id}` 为准；该接口同样支持 `next_cursor` / `after`。单个导入任务在 API 重启后可从统一 Task 快照回读，不再因 `ImportJobs` 内存清空而 404。

前端任务中心每秒刷新任务列表；单任务客户端可使用 SSE 获得约 250ms 粒度的状态变化。后端是唯一事实来源，前端不得推算状态。
