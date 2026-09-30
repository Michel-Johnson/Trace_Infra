# Trace Hunter

**保存 Agent 做过什么，让不同业务按自己的目标理解同一份记录。** 面向人和 Agent 的轨迹平台：记录、版本、存储、搜索与切片是平台核心，业务分类、评价和视图通过插件扩展。回放与训练数据闭环按路线图逐步交付。

- 平台内的人话说明：`/about.html`，可切换同一条示例轨迹的业务视角。
- 研究、参考与路线图：`/research.html`；源文档见[项目说明](docs/project/overview.md)、[中性 Trace 核心](docs/research/neutral-trace-core.md)、[研究索引](docs/research/README.md)、[Roadmap](docs/roadmap.md)。
- 本次交付见[2026-09-14 发布说明](docs/project/release-20260914.md)。Agent 先调用 `/api/v1/trace-formats` 核对具体输入 profile 与 Schema 摘要。

当前已实现标准导入、分析集 / Case / 运行对比和插件分析。训练闭环的领域、接口与实施顺序见[训练轨迹基础设施](docs/architecture/training-data-platform.md)，可校验的[生命周期协议草案](contracts/drafts/training-lifecycle-v1/README.md)尚未上线。导入和读取不自动触发计算或标记训练消费。

```bash
git clone git@code.byted.org:bitable/trace-hunter.git
cd trace-hunter
```

当前开发统一推送到 `dev`，后续改动也先进入 `dev`。`main` 暂不合入，待明确发布时再走合并流程。

```bash
uv venv --python 3.12 .venv
uv pip sync --python .venv/bin/python --require-hashes --only-binary :all: apps/api/requirements.lock
export DATABASE_URL='postgresql://trace_hunter:YOUR_PASSWORD@127.0.0.1:55432/trace_hunter'
export TRACE_HUNTER_CONTENT_DIR="$PWD/var/content"
.venv/bin/python scripts/migrate_database.py
ALLOWED_ORIGINS=http://127.0.0.1:5173 .venv/bin/python apps/api/run.py
```

另一个终端启动前端：

```bash
cd apps/web
npm ci --ignore-scripts
npm run dev
```

打开 **http://127.0.0.1:5173/**。前端为 React + TypeScript + Vite + Ant Design，API 为 FastAPI，数据独立存入 PostgreSQL。前端开发代理将 `/api` 转发到本机 8767。也可用 `npm run dev:mock` 独立开发前端，使用合成接口样例，无需数据库。

左侧常驻集合与 Case，右侧直接对比同 query_id 的多个 harness / 模型运行；切换 Case 保留勾选，技能 S 标记与工具方格顺序保留。导入、浏览不触发分析，成本、轨迹、时间分析仍由按钮单独触发。正式服务地址与运维流程见部署文档。

侧栏保留工作台、轨迹分析、轨迹库、插件、采集接入五个功能。`#/collections` 展示分析集 / 任务集列表；点击集合进入 Case 列表，点击 Case 进入运行对比。工作台显示真实统计与最近导入，采集接入提供已有 skill 的指令表单。参考 Linear 的产品列表层级，见 [前端设计约定](apps/web/DESIGN.md)。[集合命名规则](docs/guides/benchmark-naming.md)同时展示在新建表单和帮助面板中；线上集合仍遵循不可变的 catalog/1.0，未伪装实现新的 revision 管理。

旧版无构建 demo 保留在 `apps/trace-platform`，可用 `python3 scripts/serve_platform.py --seed-demo` 配合 SQLite 运行，默认 8766。它用于兼容与回归；新的平台开发入口为 `apps/web` 和 `apps/api`。历史输入原文与 hash 保持不变。

## Trace 分析 Skills 规划

以下 Skills 均处于**待开发**状态。它们面向已有 Trace 的评测与分析；只有需要重新运行 Agent、开展有 Skill / 无 Skill 等受控对照实验时，才考虑接入 Harbor 作为可选实验运行层。

### [`trace-eval-designer`](skills/trace-eval-designer/SKILL.md)（首版已实现）

与用户讨论并明确评测目标，把自然语言需求转换成可执行、可版本化的 Eval Template / Plugin Spec。

- 明确评测对象、分析粒度、样本范围、时间窗口与对照组。
- 定义指标、所需 Trace 证据、判定规则及 `pass / fail / unknown` 语义。
- 决定使用确定性规则、统计方法还是 LLM Judge，并设计校准样例。
- 生成 Trace Hunter Infra API 查询和取证方案，指出当前接口缺口。
- 支持设计 Skill 加载耗时与 Token、Skill 使用效果、工具调用合理性、行为合规性等模板。

### `trace-batch-analyzer`（待开发）

针对一批或全量 Trace 执行已经评审通过的分析模板，发现共性问题并返回可追溯的聚合结果。

- 调用 Infra 完成候选 Trace / Span 检索、关系过滤和证据读取。
- 执行错误签名聚合、异常模式发现、Cohort 对比、耗时与 Token 分布统计。
- 支持按 Skill、版本、模型、环境、命令、工具等维度分组。
- 输出命中数量、比例、覆盖率、未知项、代表样本和证据引用。
- 保证结果限定在同一 Trace、正确时序和声明的 Context 范围内，避免信息穿越。

### `trace-deep-dive`（待开发）

精读少量具有代表性的 Trace，重建执行过程，发现值得在全量数据中验证的问题和假设。

- 按完整时间线分析 Message、Skill、Tool Call、结果、错误和重试关系。
- 识别反复调用与报错、未遵循 Skill、错误决策、无效步骤和用户意图偏离。
- 严格区分已观察事实、合理推断与证据不足，禁止使用当时模型不可见的信息判断其决策。
- 为每个发现记录 Span 级证据、影响、置信度和适用边界。
- 将候选假设交给 `trace-eval-designer` 固化为模板，再交给 `trace-batch-analyzer` 做全量验证。

### `trace-analysis-reporter`（待开发）

把批量分析和精细分析结果整理为面向研发、产品或评测人员的可审计报告。

- 展示分析目标、数据范围、模板版本、方法、样本量与覆盖率。
- 汇总关键指标、异常模式、Cohort 差异、典型案例及证据链接。
- 明确未知项、数据缺失、潜在混杂因素和结论限制。
- 区分事实、推断与建议，不在报告阶段无依据地产生新结论。
- 支持输出结构化结果和人类可读报告，便于复查与后续追踪。

建议链路：用户需求 → `trace-eval-designer` → 版本化分析模板 → `trace-batch-analyzer`；抽样 Trace → `trace-deep-dive` → 候选假设 → 新分析模板；最终结果统一进入 `trace-analysis-reporter`。

## 镜像部署与独立开发

Python核心阶段已按用户确认收敛到第20轮，见[阶段交付与使用边界](docs/implementation/backend-core-stage-1.md)及[迭代台账](docs/implementation/backend-iterations.md)。新增原件/版本、查询、任务、远程执行和通知能力已在隔离环境验收；本次发布内容及服务核对方式见[交付说明](docs/project/release-20260914.md)。回放、训练消费和Rust留在后续路线图。PostgreSQL 进程需设置 TRACE_HUNTER_CONTENT_DIR 为持久共享目录；Compose 已配置独立内容卷。

按 Web、API、Backend、PostgreSQL、Plugin 与远程服务划分职责。API 和 Plugin 有独立镜像，复用 Backend 基础层；业务核心以模块方式运行在 API 和官方插件 Worker 中。PostgreSQL 独立持久化。远程服务通过 HTTP / MCP 查询固定输入、提交结果；可选 agent-tools 镜像打包导入 CLI、skills 和 MCP 桥。

- [模块、镜像与远程服务边界](docs/architecture/service-boundaries.md)
- [容器镜像、启动依赖与本地验证](deploy/containers/README.md)
- [skills 目录与分发边界](skills/README.md)
- [对外 API 与 Agent 接入](docs/api/external-access.md)

## 开始阅读

- [后端目标架构与分阶段验收](docs/architecture/backend-target-architecture.md) · [Langfuse / Discover Traces 源码与方法研究](docs/research/langfuse-traces-backend-architecture-20260912.md)
- [平台主设计：标准数据、环境、数据库与评测插件](docs/architecture/platform-contract-and-plugins.md) · [平台契约原型](contracts/drafts/platform-v1/README.md) · [官方接入指南](docs/guides/standard-trace-export.md)
- [中性轨迹导入：单条、多轮与批量](docs/architecture/neutral-trace-import.md) · [批量导入 Schema](contracts/drafts/import-v1/import.schema.json)
- [接入 Adapter 工程验证](docs/architecture/agent-friendly-ingestion.md) · [跨来源验证](docs/research/interop-validation-2026-09-10.json)
- [通用轨迹方法：论文、开源实现与 v2 设计草案](docs/rfcs/0002-general-trajectory-contract.md) · [离线契约原型](contracts/drafts/trace-v2/README.md)
- [数据接口定义与接入示例](docs/api/README.md) · [OpenAPI 3.1](docs/api/openapi.json)
- [远程部署与维护](docs/deployment.md)
- [当前架构与分层边界](docs/architecture/platform-v0.2.md)
- [依赖发布冷却期与校验](docs/security/dependencies.md)
- [标准输入协议](docs/trace-protocol-v1.md)
- [集合、Case 与多轮输入协议](docs/catalog-protocol-v1.md)
- [集合清单样例](examples/catalogs/supermarket.catalog.json)
- [JSON Schema](schemas/trace-v1.schema.json)
- [最小可导入样例](examples/minimal.trace.json)
- [采集技能共同契约](docs/collector-contract.md)
- [Doubao Work 本地采集：导出与持续快照](docs/local-doubao-collector.md)
- [Doubao 聊天页面 hook：已实测原始流采集](docs/local-doubao-renderer-hook.md)
- [Doubao 本地任务用量账目：已实测额度消耗](docs/local-doubao-usage-ledger.md)
- [真实 token 与模型耗时：获取路径、最新证据和验证顺序](docs/research/doubao-token-recovery-strategy.md)
- [缺失模型耗时与 token：本地验证和补采方案](docs/research/doubao-model-metrics-recovery.md)
- [多轮轨迹与压缩续接研究](docs/research/multi-turn-traces.md)
- [Doubao 导出、后台查询与采集验证](docs/research/doubao-export-and-capture.md)

方格默认只为三类工具上色：Read 蓝、Bash 绿、Write/Edit 橙；Skill、交互和其他调用使用中性灰，深浅对应统一耗时档位。完整色阶、主题选择和类型字母开关收在“颜色说明”。字母默认隐藏，悬停可查看类型与秒数。切片器支持工具类型多选，以及技能标记、耗时未知和错误状态的交叉筛选。读取技能文件仍属于 Read，开启字母后以 R＋S 标记；显式 Skill 调用属于独立类型。过滤保留原始顺序与编号，仅影响方格展示。

技能加载与显式 Skill 调用的 S 标记常驻在方格左上角，不受普通类型字母开关影响；悬停可区分加载或调用及技能名。

新版主题配置位于 `apps/web/src/components/TraceExplorer.tsx` 与 `apps/web/src/styles.css`。隔离类型支持沙箱 / 非沙箱 / 未知，另存联网状态、观测时间与外部工具版本。

## 转换历史导出

```bash
python3 scripts/normalize_trace.py execution-trace.json \
  --format claude-export --output run.trace.json \
  --query-id supermarket-base --env-id work-env-v1 \
  --isolation unknown --network-access unknown
```

支持 Claude Code 和本仓库已有 Doubao 导出；当前输入协议为 `trace-hunter/1.1`，兼容保留旧 1.0 输入。`examples` 包含现有五轮采集的标准化输入和一份合成最小样例。完整历史原件与旧报告位于本地 `apps/trace-lab`，不随 Git 分发；标准样例保留来源 hash。`scripts/build_examples.py` 需要这些本地原件，正常启动与标准导入不需要。

`var/`、`tmp/`、`external/` 和本机模型适配脚本均不提交。原始网络响应可能含账户信息；代码仓库只保留采集器、协议、平台与经过检查的实验摘录。外部研究源码是本地参考副本，文档中列出其来源；不作为运行时依赖。

标准样例已保存从 hash 核验原件恢复的调用顺序和技能标记，因此新克隆也能正确放置没有耗时的调用，不依赖本机原始日志。原始时间、用量与来源 hash 保持不变。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
node --test tests/*.mjs
cd apps/web
npm run build
npm audit
```

设置 `TEST_DATABASE_URL` 为专用 PostgreSQL 测试库可启用并发和迁移集成测试。测试在独立临时 schema 中运行并清理自身数据；未设置时这部分会跳过。协议修改后先运行 `scripts/build_api_contract.py`，再在前端目录执行 `npm run types`。

当前已实现 query/env 身份、环境三态、不可变导入、同 query 对比与调用详情。已有分析器只由显式请求触发，导入与浏览路径不会生成分析结果。评测插件已支持时间分析与断言验收，训练轨迹筛选和更多分析方法可继续通过插件扩展。


点击“生成三项分析”可查看成本、轨迹、时间。无单价时只显示 token；轨迹分析当前提供技能和工具统计，大模型评审尚未运行。费用估算可用 `--pricing-file config/prices.example.json` 演示，该文件是合成单价，不是实际报价。调用时长缺失的方格保留原相对位置，顺序依据可以在详情查看。

评测插件首版已实现：[接入与执行指南](docs/guides/evaluation-plugins.md)。包含官方时间分析、断言验收、独立 worker、外部 HTTP / MCP 接口与通用结果页面。

插件的总体架构扩展为[渲染、评测、分类切片的能力集合](docs/architecture/general-plugin-model.md)。同一插件可以组合多种贡献点，执行宿主与能力类型分别声明。[plugin-v2 协议草案](contracts/drafts/plugin-v2/README.md)包含纯渲染和远程 Agent 分类样例；稳定 2.0 已实现通用目录、前端视图、分类切片与类型化计算产物，见[通用插件接入指南](docs/guides/general-plugins.md)。
