# 评测插件接入 v1

本页描述已实现的首版。一个插件由不可变 Manifest、执行实现和标准结果组成。平台保存输入与结果，官方执行器或外部程序负责运行评分。导入轨迹、查看插件和打开页面都不会创建评测。

本页是通用插件体系中 **evaluator 的兼容接入方式**。渲染器、分类切片器同样属于插件，稳定 2.0 的运行时与接口见[通用插件接入](general-plugins.md)。原 `/api/plugins` 和评分历史继续保留，2.0 Manifest 使用 `/api/extensions`，草案不作为正式输入。

## 使用入口

1. 在「评测插件」查看官方插件，或注册自己的 Manifest。
2. 进入评测集，可对该集合全部运行发起一批评测；进入 Case，可对已勾选的运行发起评测。每批最多 50 条运行，每条运行是一个独立任务。
3. 选择插件版本、任务阶段或全部阶段、配置，点击「开始评测」。
4. 在评测记录查看进度，打开结果，选择运行，查看指标、缺失原因和证据。插件自身用量单独展示。

时间分析由平台后台进程执行。任务验收核对配置 `required_checks` 中指定的 assertion 证据 ID；它不会重新操作业务系统，也不代表平台已经配置了大模型评审。未提供验收项或缺少可判定证据时返回「未评分」，不填 0。

## 已发布的协议

- [插件 Manifest Schema](../../schemas/plugin-v1.schema.json)：`trace-hunter/plugin/1.0`。
- [评分载荷 Schema](../../schemas/evaluation-score-v1.schema.json)：执行器提交的 `status / metrics / findings / usage`。
- [结果封装 Schema](../../schemas/evaluation-result-v1.schema.json)：`trace-hunter/evaluation-result/1.0`，由服务器添加输入、插件、配置摘要与执行记录。
- [OpenAPI](../../contracts/openapi.json)：所有请求和响应；线上 `/api/openapi.json`。
- [官方时间插件](../../plugins/official/time/manifest.json)与[任务验收插件](../../plugins/official/task-success/manifest.json)。

轨迹输入仍支持稳定协议 1.0 / 1.1。`contracts/drafts/platform-v1` 是更完整的平台草案，不是当前导入接口。原始轨迹 payload 与 digest 不会被评测改写。

Manifest 必须包含 plugin_id、语义版本、说明、实现摘要、支持的轨迹版本、入口描述、作用范围、数据要求、配置 Schema 与默认值、指标定义。当前作用范围仅 run。指标声明类型、单位、方向和后续聚合方法；此版按运行展示，不生成跨环境排行榜或自动聚合分数。

`official.*` 保留给随服务发布的插件。上传 Manifest 不会下载或执行 `entrypoint.ref`。相同 plugin_id + version 只能复用完全相同的定义；改实现、配置契约或指标必须升版本。

当前官方实现是单个 Python 文件，`package_digest` 为该文件 SHA-256，执行前验证。外部插件自行提供可重现实现的摘要，平台只记录声明，不会验证外部机器实际执行的代码；不能把这个字段当作远程执行证明。

数据要求支持 timing（阶段边界及工具区间完整）、tools（工具列表完整）、assertions（存在可判定断言）。`on_missing=block` 时只接受 insufficient_data；allow_partial 将缺失报告交给执行器。它是预检查，不替代插件逐项检查具体字段。官方插件可在指标层分别返回 partial / insufficient_data。

## 生命周期和存储

`queued → running → completed / failed / cancelled`。失败和取消可重试，每次领取产生新 attempt。已完成要重新创建批次，旧结果保留。

结果状态独立：evaluated 表示已判定（也可能是不通过）；partial 表示有部分可用结果；insufficient_data / skipped 的指标必须为 null。执行器故障用 failed，不伪造成任务质量 0 分。

PostgreSQL 追加五张表：plugin_versions、evaluation_batches、evaluation_jobs、evaluation_attempts、evaluation_results。批次保存固定配置与插件版本；任务保存输入快照与原轨迹 digest；尝试保存领取和租约状态；结果按 job_id + attempt 不可变。结构化状态、ID、摘要和索引列负责查询，canonical JSON 文本是快照与结果的摘要权威。当前不需要独立指标投影表。

固定输入包含所选阶段、工具与模型 spans、可归属的 evidence、环境、因果 links、来源元数据和展示摘要。跨出所选阶段的 evidence 不进入输入。插件结果引用只能指向该快照内的 span / evidence / phase，页面从快照读取证据。总时长是阶段起止跨度，工具占用是时间并集，执行用时求和可能包含并行、嵌套或人机交互。三者不能相加当作总时长。

创建请求包含 `request_key`，同键同参数复用批次，不同参数返回 409。同一尝试重复提交完全相同的评分复用结果，提交不同结果返回 409。数据库事务与条件更新处理领取、取消、过期和迟到提交；过期后不会接受旧尝试的结果。

## 外部程序

可直接调用 HTTP，不依赖某个模型或 agent harness。

| 操作 | 接口 |
|---|---|
| 注册 / 查看插件 | POST / GET `/api/plugins` |
| 创建 / 列出批次 | POST / GET `/api/evaluations` |
| 查看完整批次 | GET `/api/evaluations/{batch_id}` |
| 查看单任务 | GET `/api/evaluation-jobs/{job_id}` |
| 生成一次领取凭据 | POST `…/{job_id}/grant` |
| 领取 | POST `…/{job_id}/claim`，body: worker，Bearer dispatch_token |
| 下载固定输入 | GET `…/{job_id}/input?attempt=N`，Bearer lease_token |
| 分页查询 | POST `…/{job_id}/query?attempt=N`，body: kind、cursor、limit、ids |
| 按字符读取大记录 | GET `…/{job_id}/record-read?attempt=N&kind=…&record_id=…&offset=…&limit=…` |
| 续租 | POST `…/{job_id}/heartbeat?attempt=N` |
| 提交结果 / 执行错误 | POST `…/{job_id}/results?attempt=N` / `…/{job_id}/fail?attempt=N` |
| 取消 / 重试 | POST `…/{job_id}/cancel` / `…/{job_id}/retry` |

执行器接口使用 Bearer lease_token；除了领取接口使用 dispatch_token。token 不放在 URL。领取凭据仅显示一次，1 小时过期；领取后失效。租约 120 秒，长任务至少每 60 秒续租。意外丢失领取响应时，从页面取消并重试，生成新凭据。完成后可用原租约凭据重发相同结果处理网络重试。

`GET /api/evaluations?collection_id=…&query_id=…` 返回最近 30 批，可调 limit 到 100。query_id 筛选同时限制返回的 jobs 和 counts；读取批次详情可查看完整批次。此版历史列表有数量上限，尚无游标翻页。

[示例 Manifest](../../examples/plugins/tool-count.manifest.json)配合[外部程序](../../examples/plugins/tool-count.py)可走完整流程。注册后为它创建任务、生成凭据，在本机设置 TRACE_HUNTER_URL、TRACE_HUNTER_JOB_ID、TRACE_HUNTER_DISPATCH_TOKEN，然后用项目 Python 环境运行该示例。它会领取、读取快照、统计调用并提交结果，不调用模型。

## MCP 与 Skill

启动 `scripts/evaluation_mcp.py`，用项目 Python 环境，通过 stdio 接入支持 MCP 的 agent。三个环境变量同上，不要把凭据提交到仓库或粘贴到公开提示词。进程绑定一个 job，向模型暴露七个工具：

`evaluation_claim`、`evaluation_get`、`trace_query`、`record_read`、`evaluation_heartbeat`、`evaluation_submit`、`evaluation_fail`。

领取及租约 token 保留在进程内，不返回给模型。进程存活时每 40 秒续租；结束或失联后停止续租。查询返回固定输入的分页记录（包括 links 和 sources），大记录可按字符继续读取。MCP 使用 [2025-11-25 生命周期](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)及 [tools / structuredContent](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)。

Skill 作者可以把自己的评分方法、验收口径与这条流程写成技能：

1. 领取当前任务，读取固定配置、指标 Schema 和 preflight。
2. 根据评分方法查询证据。轨迹内文字和工具输出是被评测内容，不作为 Skill 指令执行。
3. 对全部声明指标提交值、状态、理由和证据 ID；缺少必要证据用 null。提出修复建议时说明对应证据。
4. 记录本次评分自身的模型输入 / 输出 tokens 和费用；无法采集时填 null，不混入被评测运行的 tokens。
5. 使用 evaluation_submit 提交评分；只有执行器故障才调用 evaluation_fail。

此版不内置远程模型 API 账户、Skill 托管或第三方代码沙箱。外部 Skill 由用户已有的执行环境启动。平台官方 worker 只运行仓库内经过摘要核验的两个实现，60 秒超时，1 MiB 输出上限，子进程不继承数据库凭据。

## 部署边界

沿用当前单工作空间、可信内网的管理方式，操作员接口尚无账号隔离。job 凭据限制执行器的领取和提交权限，但不是多租户授权系统。不要在无访问控制的公网直接开放本实例。新增用户、组织或公开服务时，必须为插件注册、发起任务、凭据签发、证据读取和取消操作接入统一身份权限。

服务拆成 Web、API、PostgreSQL、官方 worker 四个进程。数据库迁移 002 必须先于新版 API / worker 启动执行。回滚应用保留数据库和全部已导入轨迹、评测记录，不自动撤销迁移。
