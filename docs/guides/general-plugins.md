# 通用插件接入 2.0

本版提供可运行的 renderer、slicer(filter/classify) 与 evaluator 贡献点。同一插件可以组合多种能力，评分只是其中一种。稳定定义为 `trace-hunter/plugin/2.0`，历史 draft 文件保留用于设计验证。

## 在页面使用

侧栏「插件」按 `plugin_id` 显示目录卡片，可搜索名称、用途与 ID，或按界面、评测、分类/切片筛选。卡片默认展示最新版本；所有历史版本及完整定义保留在详情的版本选择中。卡片上的「在任务中使用」进入任务列表，不创建计算任务。

在 Case 中，调用轨迹上方的「轨迹视图」切换调用方格和调用列表，两者保留相同的筛选条件、原调用序号及点击详情。工具类型/技能/状态筛选即时执行，不创建后台任务，总时长和实际用户对话轮数维持原运行口径。

选择分类插件，点击「一键分类」，后台固定所选运行与阶段并生成 facets，完成后按分类分组；也可切换到时间/原始顺序，或按工具用时、前置响应、响应＋工具用时降序排列。「时间统计」按当前分类分别汇总工具、模型请求和等待，保留计时覆盖数。「更多分析」保留自定义配置与外部计算入口。

已有[Base 阶段分类插件](base-stage-classification.md)和调用活动分类插件。完成后按具体阶段或读取/写入/执行等维度切片；切换视图后条件仍保留。「查看产物」展示分类依据并可回查原始记录。选择“不使用分类”只关闭派生筛选，不删除分类结果。

调用活动分类器按原始 operation 字段归类；Base 插件按有依据的 Agent、工具和 CLI 模块标识归类。两者都不输出质量分数。coverage=complete 表示声明的目标记录均已处理，其中仍可以存在 unknown；它不证明原始轨迹采集完整。未分类和不适用是独立状态，多标签按记录去重，不当作调用次数相加。

## 插件声明与实现

[稳定 Manifest](../../contracts/schemas/plugin-v2.schema.json)保存公共身份、包摘要和 contributes[]。每个贡献点包含 kind、implementation、scope、输入合同、配置、触发方式和输出类型；renderer 有 mounts，evaluator 有 metrics/requirements，slicer 有 filter/classify 模式。

官方实现：[manifest.json](../../plugins/extensions/call-activity/manifest.json)、[分类程序](../../plugins/extensions/call-activity/classify.py)、[渲染组件](../../apps/web/src/plugins/renderers.tsx)、[切片实现](../../apps/web/src/plugins/slicers.ts)。包摘要来自明确文件列表的逐文件 SHA-256，`scripts/build_official_extensions.py` 生成 package.json 和 Manifest。发布后的实现变更必须使用新插件版本；不要重新生成并覆盖已注册版本。

方格和列表现通过[浏览器插件宿主 1.0](../architecture/browser-plugin-host.md)加载：Cordis 管理服务依赖和清理，SlotCore 注册 `run.timeline`，React 传入当前 Run 与筛选数据。接入 renderer 时，把实现包装为 BrowserPlugin，声明 requires，在 activate 中注册 Manifest 对应的视图，并加入随 Web 发布的本地定义。完整 Manifest 与服务端目录匹配后才激活。即时 filter 控件仍使用现有切片实现，尚未迁到独立插槽。

插件卡片或详情可以加载、停用界面插件；宿主一次只激活一个提供 renderer 的插件，切换前完成旧实例清理，纯服务依赖可并行。选择保存在当前浏览器，跨导航与刷新保留；不可用版本不会执行，目录移除时可回退至其他兼容界面。计算任务和原始记录保留。`implementation.ref` 不会被当作任意远程脚本 URL 加载，未发布的浏览器实现不会声称已经可用。

分类和评测计算都可通过外部执行器接入。登记新 Manifest 后，页面即可创建其显式计算任务，再通过任务凭据由外部程序执行。当前计算入口支持每批 1–50 条 run、task/all 阶段，计算输入为轨迹快照；其他 scopes 及计算任务的派生依赖解析尚未提供，创建时明确拒绝，浏览器可消费已经生成的分类结果。

## HTTP 合同

| 入口 | 用途 |
|---|---|
| GET `/api/extensions` | 统一能力目录；含稳定 v1 评分器的只读映射 |
| POST `/api/extensions` | 注册原生 2.0 插件版本 |
| GET `/api/extension-schema` | 稳定 Manifest Schema |
| GET `/api/plugin-output-schema` | 分类或评分的类型化输出 Schema |
| GET / POST `/api/plugin-runs` | 查询 / 创建原生计算批次 |
| GET `/api/plugin-runs/{batch_id}` | 批次与运行任务 |
| `/api/plugin-jobs/{job_id}/…` | 领取、查询、续租、提交、取消和重试 |

job 操作沿用原评测接口的 grant、claim、input、query、record-read、heartbeat、results、fail、cancel、retry；详情见 [OpenAPI](../../contracts/openapi.json)。创建请求在旧评测参数上增加 contribution_id。非 explicit 的 renderer/filter 不允许创建任务。已有 `/api/plugins`、`/api/evaluations`、`/api/evaluation-jobs` 继续服务稳定 v1 评分器，不改写旧 Manifest、digest 或历史结果。

分类提交为 `{kind: "facets", data: FacetSet, usage: …}`，其中 FacetSet 遵守[分类合同](../../contracts/schemas/facets-v1.schema.json)。评分提交为 `{kind: "evaluation", data: EvaluationScore}`，复用已有[评分合同](../../contracts/schemas/evaluation-score-v1.schema.json)。输入不足的评分仍为 null；分类使用 unknown，不强制填写分数。

服务端校验输出种类、输入摘要及 selection_digest、实体与证据归属、维度及值定义、单/多选约束和完整覆盖声明。默认分类目标仍是本次范围中的工具 span。新插件可在 `extensions["trace_hunter.classification_targets"]` 按贡献点 ID 声明 span kind 列表，例如 `{"classify":["tool","model","wait","agent"]}`。此扩展只允许 classify 贡献点，服务端按声明校验成员及 complete 覆盖；不改变原输入范围，也不改变旧插件的工具范围。其他运行或未选阶段的引用始终拒绝。

`input_ref` 由 input/plugin_get 接口提供，执行器原样引用；selection_digest 当前使用包含固定阶段、记录、来源和展示摘要的 snapshot_digest。

评分和分类产物都由服务器封装为 `trace-hunter/plugin-result/1.0`，绑定插件版本、贡献点、输入范围、配置与尝试。原始 trace 不更新。插件自身 token/费用独立于被分析轨迹。纯渲染和即时切片不产生计算产物。

## 远程 Agent

桥接进程运行在 Agent 所在机器或容器，使用项目已锁定的 Python 环境启动 `scripts/evaluation_mcp.py`。任务详情的“生成接入凭据”提供：

```text
TRACE_HUNTER_URL=<远程机器可访问的平台地址>
TRACE_HUNTER_JOB_ID=<本次 job_id>
TRACE_HUNTER_DISPATCH_TOKEN=<一次领取凭据>
TRACE_HUNTER_JOB_KIND=plugin
```

plugin 模式暴露 plugin_claim、plugin_get、trace_query、record_read、plugin_heartbeat、plugin_submit、plugin_fail。plugin_get 返回本次贡献点、配置、固定输入摘要、input_ref 和输出 Schema。这样同一 Agent 可以执行分类 Skill 或评分 Skill，各自返回对应类型的产物。凭据保留在桥接进程中，领取后 120 秒租约，进程存活时每 40 秒续租；过期和取消的尝试不能回写结果。

不设 JOB_KIND 时继续使用兼容的 evaluation 模式。该 MCP 仍为 stdio，由 Agent 启动桥接；跨机器通信使用 HTTP。平台尚未提供 Streamable HTTP MCP，也尚未对接任意远程 Agent 服务的自动启动/取消 API。外部服务在自身工作环境领取和运行，不能把注册 Manifest 理解为已经启动远程 Agent。

## 存储与运行

迁移 003 添加 extension_versions（原生 Manifest）、plugin_handlers（不可变计算定义）、plugin_batches、plugin_invocations、plugin_attempts、plugin_artifacts。legacy evaluator 保持原表，二者共用 TaskLifecycle 的领取、租约、取消、重试与幂等逻辑。renderer/filter 不进入这些任务表。

现有独立 worker 公平轮询旧评分任务与原生贡献点，只执行随代码发布且摘要一致的官方程序。外部计算使用任务接口回写，服务器不执行上传的代码。应用仍沿用可信内网单工作空间的权限边界，注册权限与 job 凭据不是多租户访问控制。
