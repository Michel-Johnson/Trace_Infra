# Trace Hunter 内置 Agent

内置 Agent 使用独立 Claude Code CLI worker、现有 Trace Hunter CLI/Skill 和 HTTP API。每个前端会话绑定一个用户选择的项目；一次用户消息对应一个 Claude 回合，回合封存为一个不可变 v2 Trace。用户上传 Trace 后，Agent 自动分析来源格式、选择或开发 Adapter 并导入；业务评测仍由用户单独发起。

## 网页自动导入

网页选择文件后统一走内容摘要校验和可续传分块上传。`source_format=auto` 的上传完成会保存原件，创建持久化 `adapter_import` 任务和 Agent 会话，并将原件作为会话附件送入隔离 worker。Agent 检查已注册格式和已保存脚本，验证映射后导入并写 `import-result.json`。worker 只在实际 revision 的索引完成、来源 SHA-256 与上传原件一致时将任务标为成功；开发的脚本以内容引用保存，可按项目列出与回读。服务重启后原件和任务仍可查；中断的任务标失败，经显式重试创建新 Agent 回合。导入页“查看运行记录”使用顶部 Agent 面板，和交互式 Claude 会话共用历史选择器及 Mulmo xterm 画布。导入会话由此画布只读回放持久事件、阶段及 Trace，不连接 PTY、不恢复或新建 Claude 进程，也不能输入或续聊。

涉及接口：`POST/GET /api/v1/projects/{project_id}/imports/uploads`、`PUT .../parts/{position}`、`POST .../complete|retry|cancel`、`GET .../tasks/{task_id}`、`GET .../import-adapters` 和 `GET .../import-adapters/{digest}/content`。CLI 的 `upload` 默认使用此流程；显式 `--source-format` 保留已注册 Adapter 的直达路径。浏览器 guest cookie 覆盖 `/api/v1`，供上传请求与 Agent 会话使用同一归属身份。

## 当前状态与权限边界

飞书企业应用登录暂缓。`TRACE_HUNTER_AGENT_ENABLED` 默认关闭。开发态开启后，服务会向浏览器发放 HttpOnly、SameSite=Strict 的随机 guest cookie，使不同浏览器不会在 Agent 会话列表中混用记录。**这不是用户认证**：现有 operator API 仍可读取 Agent Trace 项目，不能宣称个人 Trace 私密，也不能在公网或多租户环境启用。`/api/v1/agent/capabilities` 明确返回 `identity=browser_guest` 与 `private_project_isolation=false`。启用正式服务前必须完成飞书认证与项目授权隔离，或者由产品负责人明确接受只在受控内网试运行的边界。

模型凭据与 worker 令牌只可放在服务器私有环境文件，不进入仓库、Trace 或前端。CLI 固定经过发布日期及完整性校验的 `@anthropic-ai/claude-code@2.1.274`；安装忽略 npm 生命周期脚本。worker 启动时验证版本，缺少模型配置或 worker 令牌则拒绝启动。Claude 进程可读取模型令牌，因此必须以独立系统用户、只读源码及受控网络出口运行。

## 接口

| 方法与路径 | 用途 |
|---|---|
| `GET /api/v1/agent/capabilities` | 功能、身份和隐私边界；开发态初始化浏览器 guest cookie |
| `POST/GET /api/v1/agent/sessions` | 新建和列出当前浏览器的会话；列表包含 `kind`、最近回合状态、任务 ID、附件文件名，供统一选择器展示 |
| `GET /api/v1/agent/sessions/{id}` | 会话、回合、附件与任务状态 |
| `POST /api/v1/agent/sessions/{id}/messages` | 排队一个回合，同会话同时只允许一个运行中回合 |
| `POST /api/v1/agent/sessions/{id}/cancel` | 取消排队或请求终止运行中的回合 |
| `PUT /api/v1/agent/sessions/{id}/attachments/{filename}` | 附加文件，原件进入内容寻址存储 |
| `POST /api/v1/agent/worker/turns/{turn_id}/sources/{source_id}` | worker 专用：流式登记当前回合的模型正文原件 |
| `POST /api/v1/agent/worker/turns/{turn_id}/otel/authorize` | worker 专用：登记单回合遥测令牌的摘要，不向 CLI 暴露 worker 令牌 |
| `POST /api/v1/agent/worker/turns/{turn_id}/otel/v1/{signal}` | CLI 专用：用单回合令牌写入 OTLP HTTP/JSON 日志或 Trace，限 8 MiB/批 |
| `GET /api/v1/agent/worker/turns/{turn_id}/otel` | worker 专用：有界回读原始遥测并归档到本回合；超 32 MiB 标记 partial |
| `GET /api/v1/projects/{project_id}/traces/{run_id}/revisions/{revision}/sources/{source_id}/content` | 项目读取权限：回读本 Trace 已封存的原始模型正文 |
| `GET /api/v1/agent/sessions/{id}/events` | 持久化序号 SSE；支持 `Last-Event-ID` 或 `after` 续接 |
| `GET /api/v1/agent/sessions/{id}/events/page` | 同一事件日志的 JSON 分页回读 |

`/api/v1/agent/worker/*` 以及附件原件读取、回合归档接口要求专用 `X-Agent-Worker-Token`；不要将该令牌交给浏览器。公开 API 和类型由 `contracts/openapi.json` 与 `apps/web/src/api/generated.ts` 描述。worker 通过这些内部 HTTP 接口领取/完成任务，不直接打开 API 的会话数据库。

## Trace 采集与覆盖

Agent Trace Recorder 是只加载到内置 worker 的 Claude Code 插件，合并 Claude `stream-json`、Hook、子 Agent transcript 与本机 OTLP 遥测。Claude 通过回环地址将 OTLP HTTP/JSON 发给现有 Core API `127.0.0.1:8767`，每回合使用独立的高熵令牌；API 将原始遥测写入内容寻址存储，不发送第三方。模型原始请求/响应采用 Claude 本地文件模式，逐个文件流式写入 Trace Hunter 内容寻址存储；不安装 LangSmith SDK。一个回合只追加一个 `agent-<turn_id>` 不可变 Trace revision，含消息、模型/工具/子 Agent Span、实际请求 Context 和来源指针。原件摘要可回读核对，旧 Trace 不因 Recorder 更新而新增 revision。

子 Agent 的实时文本不保证覆盖全部内部执行，因此 Hook 的 `SubagentStop.agent_transcript_path` 用于回合末补充。不存在的 transcript 标 `missing`，超过预算标 `partial`。只有原始 API 请求文件且与模型 Span 的消息 ID 匹配时，Context 才记为实际请求；否则维持 `missing/partial`，不从聊天历史推断。Claude 会遮蔽扩展思考内容，不能宣称未遮蔽的完整采集。前端事件只提供受限预览；Trace 页面按需从项目归属的来源接口回读完整模型输入/输出。worker 中断后会封存已落盘的部分 Trace，不重放工具命令。

Claude 2.1.274 的 `api_request` 日志没有请求 ID。Recorder 用 `api_response_body.message.id` 关联 stream 模型调用，再以响应时间唯一匹配 `llm_request` OTLP Span；候选并发或距离超阈值时保留 `partial`，不猜造耗时。封存后到达的遥测返回 409，并在会话事件日志留下 `recorder_telemetry_late` 审计记录，不修改已封存 revision。

Claude 的 `input_tokens` 仅包含未缓存部分；canonical 模型 Span 的总输入量为该值加缓存读取与缓存写入，缓存计数仍单独保留。只有全零占位的 stream 用量记为 unknown，能唯一关联的逐请求 OTLP 用量才写入该 Span；不得把回合总量分摊给请求。Claude Bash 工具可能不继承 worker 的 `BASH_ENV`，因此独立的 PreToolUse 守卫仅为包含 Trace Hunter CLI 且带管道的 Bash 命令加 `set -o pipefail`；Recorder 采集 Hook 仍只记录、不改写。即使外层 shell 把退出码吞掉，转换器也依据 CLI 的精确结构化错误行将工具 Span 标为错误，并记录该判定来源。

`stream-json` 日志在单回合达到 32 MiB 预算后停止写入超限记录，落盘 `stream_budget` 标记并在 Trace 的 `stream_coverage` 报告缺口；已保存的记录继续参与封存。模型原始请求/响应走独立文件和内容引用，不受这一预览流预算截断。worker 重启后的恢复路径会识别落盘标记，不把部分日志误报为完整。

原始请求可能包含全部历史对话、文件正文和密钥。当前 Agent 仍为 guest cookie，没有个人 Trace 访问隔离；用户已选择让能打开对应 Trace 的访问者在网页回读原文。仅可在受控内网使用，页面会提示风险，不得标注为私密或开放到公网。

## 验收口径

合成测试覆盖浏览器会话分离、worker 内部鉴权、附件回读、事件序号、模拟 Claude 流、取消、服务重启后的部分 Trace 归档及来源引用校验。Recorder 的真实回合验收不等于“上传文件→导入→检索→分析”整条业务链的验收，不能用合成测试冒充。飞书登录、CLI 浏览器授权、匿名 API 拒绝与个人 Trace 隔离在认证暂缓期间均未验收。

2026-09-24 在 10.37 以合成提示词执行过真实 Claude CLI 回合：2 个模型 Span、1 个成功工具 Span、2 个实际请求 Context，6 个模型正文来源和 6 个 OTLP 原始批次。来源摘要全部可核验，两个模型 Span 的起止时间均来自唯一匹配的 OTLP Span；网页按需展示原始请求/响应并提示无个人访问隔离。子 Agent、工具失败/Skill、压缩、取消、重启和多回合目前由合成测试覆盖，未宣称在真实模型会话逐项验证。浏览与导入仍不触发分析。
