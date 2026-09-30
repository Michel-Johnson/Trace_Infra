---
name: trace-hunter-cli
description: 使用 Trace Hunter 平台导入、检索和分析 Trace。适用于需要通过平台 CLI 查询 Trace/Span/Skill、搜索正文、分析耗时或回溯证据的任务；不用于绕过平台直接查询数据库。
---

# Trace Hunter CLI

## 版本预检

版本页：[查看最新 Skill](http://10.37.24.3:8766/skills.html)；机器接口：[查询版本](http://10.37.24.3:8766/api/skills/manifest)；[下载官方包](http://10.37.24.3:8766/api/skills/archive)。仅在服务器的 tracehunter-agent 隔离 worker 中，若公网入口连接拒绝或超时，改用 [worker 版本接口](http://127.0.0.1:8767/api/skills/manifest) 和 [worker 下载接口](http://127.0.0.1:8767/api/skills/archive)；HTTP 错误不能靠切换地址绕过。

每次使用前确认接口返回 HTTP 200 且有本 Skill 的版本条目，下载同一入口的官方包，逐文件对比本 Skill 与 CLI 脚本。空输出、非零退出码或无法读取压缩包都不是通过；记录检查结果。安装态有更新时保留用户改动并更新、校验；仓库开发态只报告差异，不用线上包覆盖源码。无法检查或更新时停止。

使用仓库中的 `scripts/trace_hunter_cli.py` 与 Trace Hunter Core API 工作。把用户目标拆成可验证的查询，不把自然语言原样塞进某一个搜索框。

普通只读查询、需求澄清和 Eval Spec 设计直接进行，不创建工作台任务。对于用户准备启动的多阶段执行 workflow，先按 Router Skill 提出任务草案并取得“创建任务并开始”的肯定答复；这不是重新确认已经明确的业务需求。得到答复前不执行该 workflow。

## 开始

1. 下载包安装时，将本 `SKILL.md` 所在目录记为 `SKILL_ROOT`，使用 `$SKILL_ROOT/scripts/trace_hunter_cli.py`；仓库开发时使用根目录同名脚本。解释器优先用 `.venv/bin/python`，不存在时使用 `python3`。
2. 在 Trace Hunter 内置 Claude 终端，始终使用 `--profile terminal`，由专用配置选择服务与项目；不要读取下载包的 `service.json` 来覆盖它。仅外部安装态按 [服务配置](references/service.json) 连接；用户明确指定其他服务时另行核对目标与权限。
3. 终端运行 `python3 scripts/trace_hunter_cli.py --profile terminal capabilities`；外部安装态才用 `--url URL --project PROJECT`。以实际服务声明的 Trace、Span、搜索、对象查询与指标、导入和任务能力为准。完整命令见 [CLI 参考](references/cli.md)。

## 评测只使用六种取数入口

| 需要 | CLI 命令 |
|---|---|
| 固定样本及版本 | trace query |
| 读取结构化证据、工具参数和结果 | object-query |
| 读取关键步骤的前后文 | span-window |
| 搜索正文中的具体线索 | search |
| 统计耗时、调用量、错误率 | metrics-query |
| 批量取得固定选集的正文证据 | evidence-export |

结果质量使用目标、最终输出与验收证据；执行过程使用用户/Skill 要求与实际步骤；工具调用使用工具定义、参数、返回和后续处理；效率与稳定性使用指标和错误前后文。同一证据可复用，不为四个维度重复下载。

对象正文优先选择 payload 字段；正文投影需要完整读取时导出证据。窗口和搜索片段有截断标记，不能当成完整证据。指标接口只能统计已采集事实，任务通过率由评测结果汇总，不能用 status=ok 替代验收通过。

## 配套操作

- 导入与 Adapter：upload、import、import-adapters、import-adapter-download。旧 adapter-import 命令仅作 upload 的兼容包装，不是额外 HTTP 入口。
- 普通浏览：trace history/get/content/index、span、trace aggregate；评测优先使用上表，只有必要时回读原件。
- 任务状态：task create/update/watch/cancel/retry，登记已获用户同意的执行 workflow；快速只读查询不创建任务。
- 连接、版本、capabilities 和搜索索引维护属于基础设施，不是额外的评分入口。

复杂问题通常需要组合调用。用 `(project_id, run_id, revision, span_id)` 连接 Span 与文本命中；不要按数组位置、标题或相似名称关联。字段含义、作用域和证据规则见 [查询模型](references/query-model.md)。

## 执行规则

- 先缩小结构化范围，再搜索正文。使用 `--all-pages`，或明确说明只分析了当前页。
- `analysis` 搜索可跨已授权 Trace；`model_context` 必须提供准确的可见性目标，不能用 analysis 结果冒充模型当时可见内容。
- `snippet` 是受限片段；`snippet_truncated` 或 `text_state=truncated` 时不能宣称看过完整正文。
- 缺失耗时、来源或正文不是零或空结论。输出未知项和覆盖边界。
- 标准 v2 文件用 `import`；已注册来源 Adapter 的原文件用 `upload --source-format FORMAT --wait`，让导入、切分和索引状态同步到前端。未知格式先走 Adapter Skill，不得用错误格式强行导入。
- 原始文件统一使用 `upload --wait`，不要截断正文或绕过 Adapter；相同文件与幂等键会续传缺失分块。批量导入应同时传 `--batch-id` 和 `--batch-total`，使网页能展示整体进度。
- 用户同意创建任务后，为本次执行生成一次 UUIDv4，调用 `task create KIND TITLE --request-key workflow:UUID --body '{"steps":[{"id":"prepare","label":"准备"},{"id":"verify","label":"核验"}]}'` 并保存返回的 `task_id`；实际阶段按获批计划填写。`KIND` 按实际流程选 `evaluation`、`analysis` 或 `custom`。网络重试复用同一 UUID；新一轮获批执行必须用新 UUID，不能只用标题、Spec、日期或 Run ID，也不使用 CLI 默认的标题派生键。跨 Skill 或调用导入子作业时沿用原任务，不另建工作台任务；已有对应任务则复用。
- 在有可核查进展的阶段边界执行 `task update TASK_ID --body '{"state":"running","current_stage":"prepare","progress":0.1}'`；只报告已完成的工作和真实数量，最终写入 `succeeded`、`failed` 或 `cancelled`。不要按 token 或循环频繁上报，也不要在只得到子作业受理回执时标记成功。任务接口失败应向用户说明，不假称工作台已有进度。
- 评测时必须使用 `scope.mode=model_context` 和准确 `visible_to`；`fail/unknown` 不得回退到 `analysis`。事后诊断才使用 `analysis`。
- 评测 Agent 只能连接配置为 evaluator-only 的 URL，并通过 `TRACE_HUNTER_TOKEN` 认证；若 Span、Trace 原件或 analysis scope 仍可访问，说明入口配置错误，应停止评测而不是继续。

## 交付证据

回答中说明项目、revision 范围、过滤条件、样本数及分页状态。每个关键结论至少带 `run_id`、`revision`、`span_id`（适用时）、耗时和 `source_refs` 或命中片段。区分平台返回的事实与自行计算的统计。
