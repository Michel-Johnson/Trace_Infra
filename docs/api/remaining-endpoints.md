# 删除后保留的全部 HTTP 接口

根据 Core API 4.0.0 生成，共 78 个方法/路径组合；其中评测取证和统计为六种能力、七个操作，其余为项目、浏览、导入、会话、版本和运维接口。接口存在不代表所有部署都启用了相关 Agent 运行时。

详细取舍见[评测接口说明](evaluation-surface.md)。路径以当前 [OpenAPI](../../contracts/openapi.json) 为准。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/health` | 检查服务与数据库连通性 |
| GET | `/api/openapi.json` | 查询机器可读接口合同 |
| GET | `/api/skills/archive` | 下载官方 Skill 包 |
| GET | `/api/skills/manifest` | 查询发布 Skill 版本与目录摘要 |
| GET | `/api/schema` | 查询 Trace 格式及 Schema 定义 |
| GET | `/api/v1/trace-formats` | 查询 Trace 格式及 Schema 定义 |
| GET | `/api/v1/trace-formats/{profile_id}` | 查询 Trace 格式及 Schema 定义 |
| GET | `/api/examples` | 读取 Trace 格式示例 |
| GET | `/api/examples/{name}` | 读取 Trace 格式示例 |
| POST | `/api/v1/projects/{project_id}/traces` | 导入标准 Trace 并生成版本 |
| GET | `/api/v1/projects/{project_id}/traces/{run_id}/revisions` | 查询 Trace 版本历史及元数据 |
| GET | `/api/v1/projects/{project_id}/traces/{run_id}/revisions/{revision}` | 查询 Trace 版本历史及元数据 |
| GET | `/api/v1/projects/{project_id}/traces/{run_id}/revisions/{revision}/content` | 读取不可变 Trace 原件 |
| POST | `/api/v1/projects/{project_id}/traces/{run_id}/revisions/{revision}/index` | 显式重建指定版本索引 |
| POST | `/api/v1/projects` | 创建、列出或确认项目 |
| GET | `/api/v1/projects` | 创建、列出或确认项目 |
| GET | `/api/v1/projects/{project_id}` | 创建、列出或确认项目 |
| GET | `/api/v1/query-capabilities` | 查询对应功能的字段、限制与能力 |
| POST | `/api/v1/projects/{project_id}/traces/query` | 筛选 Trace 样本与准确版本 |
| GET | `/api/v1/span-query-capabilities` | 查询对应功能的字段、限制与能力 |
| POST | `/api/v1/projects/{project_id}/spans/query` | 按结构化条件读取 Span，供轨迹浏览使用 |
| POST | `/api/v1/projects/{project_id}/spans/window` | 读取固定步骤的前后文和来源关系 |
| GET | `/api/v1/trace-search-capabilities` | 查询对应功能的字段、限制与能力 |
| POST | `/api/v1/projects/{project_id}/search` | 在正文中检索 literal/regex 线索 |
| GET | `/api/v1/projects/{project_id}/search/terms` | 维护既有搜索热词索引，不执行评分 |
| POST | `/api/v1/projects/{project_id}/search/terms/promote` | 维护既有搜索热词索引，不执行评分 |
| POST | `/api/v1/projects/{project_id}/search/terms/demote` | 维护既有搜索热词索引，不执行评分 |
| POST | `/api/v1/projects/{project_id}/traces/aggregate` | 汇总 Trace 元数据与覆盖，供工作台使用 |
| GET | `/api/v1/adapter-import-capabilities` | 查询对应功能的字段、限制与能力 |
| GET | `/api/v1/projects/{project_id}/import-batches/{batch_id}` | 启动格式转换导入或读取导入状态 |
| GET | `/api/v1/projects/{project_id}/import-adapters` | 列出或读取已归档的 Adapter 脚本 |
| GET | `/api/v1/projects/{project_id}/import-adapters/{digest}/content` | 列出或读取已归档的 Adapter 脚本 |
| POST | `/api/v1/projects/{project_id}/imports/uploads` | 大文件分块上传、续传、完成与自动导入恢复 |
| GET | `/api/v1/projects/{project_id}/imports/uploads/{upload_id}` | 大文件分块上传、续传、完成与自动导入恢复 |
| DELETE | `/api/v1/projects/{project_id}/imports/uploads/{upload_id}` | 大文件分块上传、续传、完成与自动导入恢复 |
| PUT | `/api/v1/projects/{project_id}/imports/uploads/{upload_id}/parts/{position}` | 大文件分块上传、续传、完成与自动导入恢复 |
| POST | `/api/v1/projects/{project_id}/imports/uploads/{upload_id}/complete` | 大文件分块上传、续传、完成与自动导入恢复 |
| POST | `/v1/traces` | 接收 OpenTelemetry Trace 采集 |
| GET | `/api/v1/agent/capabilities` | 网页 Agent 会话、消息、附件、执行状态与展示标签 |
| GET | `/api/v1/agent/sessions` | 网页 Agent 会话、消息、附件、执行状态与展示标签 |
| POST | `/api/v1/agent/sessions` | 网页 Agent 会话、消息、附件、执行状态与展示标签 |
| POST | `/api/v1/agent/trace-labels` | 网页 Agent 会话、消息、附件、执行状态与展示标签 |
| GET | `/api/v1/agent/sessions/{session_id}` | 网页 Agent 会话、消息、附件、执行状态与展示标签 |
| POST | `/api/v1/agent/sessions/{session_id}/messages` | 网页 Agent 会话、消息、附件、执行状态与展示标签 |
| POST | `/api/v1/agent/sessions/{session_id}/cancel` | 网页 Agent 会话、消息、附件、执行状态与展示标签 |
| PUT | `/api/v1/agent/sessions/{session_id}/attachments/{filename}` | 网页 Agent 会话、消息、附件、执行状态与展示标签 |
| GET | `/api/v1/agent/sessions/{session_id}/attachments/{attachment_id}/content` | 网页 Agent 会话、消息、附件、执行状态与展示标签 |
| POST | `/api/v1/agent/sessions/{session_id}/turns/{turn_id}/complete` | 网页 Agent 会话、消息、附件、执行状态与展示标签 |
| GET | `/api/v1/projects/{project_id}/traces/{run_id}/revisions/{revision}/sources/{source_id}/content` | 读取封存的原始来源内容 |
| GET | `/api/v1/agent/sessions/{session_id}/events` | 网页 Agent 会话、消息、附件、执行状态与展示标签 |
| GET | `/api/v1/agent/sessions/{session_id}/events/page` | 网页 Agent 会话、消息、附件、执行状态与展示标签 |
| POST | `/api/v1/agent/worker/turns/{turn_id}/otel/authorize` | 服务器 worker 内部执行、进度与记录归档（非评测取证入口） |
| POST | `/api/v1/agent/worker/turns/{turn_id}/otel/v1/{signal}` | 服务器 worker 内部执行、进度与记录归档（非评测取证入口） |
| GET | `/api/v1/agent/worker/turns/{turn_id}/otel` | 服务器 worker 内部执行、进度与记录归档（非评测取证入口） |
| POST | `/api/v1/agent/worker/turns/{turn_id}/sources/{source_id}` | 服务器 worker 内部执行、进度与记录归档（非评测取证入口） |
| POST | `/api/v1/agent/worker/recover` | 服务器 worker 内部执行、进度与记录归档（非评测取证入口） |
| POST | `/api/v1/agent/worker/claim` | 服务器 worker 内部执行、进度与记录归档（非评测取证入口） |
| POST | `/api/v1/agent/worker/turns/{turn_id}/events` | 服务器 worker 内部执行、进度与记录归档（非评测取证入口） |
| GET | `/api/v1/agent/worker/turns/{turn_id}/cancel-requested` | 服务器 worker 内部执行、进度与记录归档（非评测取证入口） |
| POST | `/api/v1/agent/worker/turns/{turn_id}/finish` | 服务器 worker 内部执行、进度与记录归档（非评测取证入口） |
| POST | `/api/v1/agent/worker/turns/{turn_id}/task` | 服务器 worker 内部执行、进度与记录归档（非评测取证入口） |
| POST | `/api/v1/agent/worker/turns/{turn_id}/native-terminal` | 服务器 worker 内部执行、进度与记录归档（非评测取证入口） |
| POST | `/api/v1/agent/worker/turns/{turn_id}/adapter-artifact` | 服务器 worker 内部执行、进度与记录归档（非评测取证入口） |
| GET | `/api/v1/agent/worker/sessions/{session_id}/attachments` | 服务器 worker 内部执行、进度与记录归档（非评测取证入口） |
| GET | `/api/v1/task-capabilities` | 查询对应功能的字段、限制与能力 |
| POST | `/api/v1/projects/{project_id}/tasks` | 登记/更新任务，读取状态、事件、取消或恢复支持的任务 |
| GET | `/api/v1/projects/{project_id}/tasks` | 登记/更新任务，读取状态、事件、取消或恢复支持的任务 |
| GET | `/api/v1/projects/{project_id}/tasks/{task_id}` | 登记/更新任务，读取状态、事件、取消或恢复支持的任务 |
| PATCH | `/api/v1/projects/{project_id}/tasks/{task_id}` | 登记/更新任务，读取状态、事件、取消或恢复支持的任务 |
| POST | `/api/v1/projects/{project_id}/tasks/{task_id}/cancel` | 登记/更新任务，读取状态、事件、取消或恢复支持的任务 |
| POST | `/api/v1/projects/{project_id}/tasks/{task_id}/retry` | 登记/更新任务，读取状态、事件、取消或恢复支持的任务 |
| GET | `/api/v1/projects/{project_id}/tasks/{task_id}/events` | 登记/更新任务，读取状态、事件、取消或恢复支持的任务 |
| GET | `/api/v1/advanced-query-capabilities` | 查询对应功能的字段、限制与能力 |
| POST | `/api/v1/projects/{project_id}/objects/query` | 读取固定快照的对象与 payload 证据 |
| POST | `/api/v1/projects/{project_id}/metrics/query` | 统计数量、耗时分布、错误率与已采集用量 |
| POST | `/api/v1/projects/{project_id}/evidence-exports` | 创建批量证据导出任务 |
| GET | `/api/v1/projects/{project_id}/evidence-exports/{task_id}/content` | 下载已完成证据文件 |
| GET | `/api/v1/projects/{project_id}/observability` | 检查存储、投影与字段覆盖 |
