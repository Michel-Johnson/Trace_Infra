# 评测插件的首版实现方案

> 实现状态：首版插件注册、固定输入评测、独立 worker、HTTP / MCP 接口及结果页面已落地，实际边界和用法见 [插件接入指南](../guides/evaluation-plugins.md)。本文仍保留后续设计，未全部发布。

状态：评测贡献点的实现提案，2026-09-10，部分已上线。本文仅讨论通用插件中的 evaluator 子集；renderer 与 slicer 见[通用插件架构](general-plugin-model.md)。实际已发布的接口以[接入指南](../guides/evaluation-plugins.md)为准。

## 1. 核心边界

**评测贡献点 = 版本化声明 + 评分实现 + 标准评分结果。平台负责事实与执行记录，evaluator 负责评价方法。通用插件可以提供多种贡献点，不必包含评分器。**

MCP 是平台向 agent 暴露查询和提交能力的协议；Skill 是一种评分实现的说明书。程序插件走 HTTP/SDK，Skill 插件由 agent 执行并走 MCP，二者遵守同一输入、身份、任务和结果约束。

| 平台固定能力 | 插件可扩展能力 |
|---|---|
| 原始轨迹、Case、环境、产物的版本化读取 | 任务完成度、技能使用、无效调用、修复建议等方法 |
| 时间区间、用量与缓存字段的统一口径和缺失说明 | 根据这些事实计算指标、应用价格、调用 judge |
| 创建评测、数据预检、进度、重试、取消、结果校验 | 声明所需事实、配置项、指标、证据和解释 |
| 指标表、问题列表、原始证据跳转、历史版本 | 新增评分维度，不改轨迹事实或核心数据表 |

导入、浏览、启用插件都不启动评测。评测集可以关联默认插件配置，但运行由用户显式触发。插件生成的标签是带来源及版本的派生结果，不覆盖采集标签。

## 2. 插件怎么交付

首版支持已有草案的两类入口：`external_program`、`agent_skill`。调用 LLM 是实现能力，可以出现在两类入口中；不再额外定义一套“LLM 插件协议”。

发布产物由两个对象组成：

```text
release/
  manifest.json       # 声明、配置 Schema、指标定义、实现包的摘要
  evaluator.tar.gz     # 评分程序或 SKILL.md、rubric、依赖锁、样例与自测
```

`package_digest` 指向实现归档的原始字节摘要；manifest 单独登记，避免把包含自身摘要的文件重新打入待计算摘要的对象。注册时对 manifest 另存内容摘要。同一 `plugin_id + version` 的声明或实现改变都拒绝覆盖，需发布新版本。注册描述不等于平台已经执行或信任实现。

沿用当前 [插件 Schema](../../contracts/drafts/platform-v1/platform.schema.json) 中的字段：

| 字段 | 用途 |
|---|---|
| `plugin_id / version / package_digest` | 确定这次用了哪份评分实现 |
| `trace_versions / scopes` | 支持的数据协议，以及 run / turn / span / comparison 范围 |
| `entrypoint` | 程序入口，或执行 Skill 的 agent 入口 |
| `requirements` | 需要哪些数据域，缺少时阻断、跳过还是允许部分结果 |
| `permissions` | 需要轨迹、产物、私有验收标准、模型调用或结果提交等能力 |
| `config_schema` | 表单配置，如价格版本、阈值或 judge 配置 |
| `metrics` | 指标名称、类型、单位、范围、方向、聚合方法 |
| `metadata` | 展示名称、说明、标签和扩展信息 |

现有 [任务验收插件样例](../../examples/drafts/platform-v1/plugin-task-success.json) 和 [缺产物结果](../../examples/drafts/platform-v1/result-insufficient-data.json) 是合法草案示例；摘要使用零占位，不可当作可安装发布包。

首版配置表单支持 JSON Schema 的有限子集：字符串、数值、布尔、枚举、对象及必填项。不支持的配置使用 JSON 编辑器并明确提示，不静默忽略规则。服务端执行完整的配置校验；Schema 引用仅解析已登记的依赖。

## 3. 一次评测怎么跑

```mermaid
flowchart LR
  A[选择轨迹和插件版本] --> B[固定输入与配置]
  B --> C[检查所需数据]
  C --> D[创建任务并分配执行凭据]
  D --> E[外部程序或 Skill Agent]
  E --> F[按需读取轨迹与证据]
  F --> G[提交指标和发现]
  G --> H[平台校验并展示]
```

1. 用户在集合、Case 或运行页选择插件与配置。集合批量评测先解析成明确的 Case / Run 清单并保存快照，后来导入的轨迹不会混入已经开始的批次。
2. 固定输入内容摘要、插件版本与实现摘要、Case/验收标准、环境记录、配置、judge 和价格等依赖。运行之间的对比使用明确的成员列表和顺序。
3. 数据预检使用实际可读字段和产物，不只信任采集端的完整度声明。数据不全仍可入库，但对应指标显示未评分或部分结果。manifest 的 requirements 表达共同启动门槛；价格等仅部分指标需要的依赖不设为全局阻断，缺少时由指标级状态反映。
4. 平台创建 job；外部 worker 认领一次 attempt，持有有期限的凭据。agent 也通过相同任务上下文执行 Skill。
5. 评分过程自己的时间、token、费用和执行轨迹归属于评测 attempt；不计入被测模型的原始轨迹。
6. 平台核验结果的输入、指标类型、取值范围与证据指向。成功结果不可变；重新评测创建新 job。

执行状态和评分结论分开：插件正常执行后可以得到“不通过”；插件异常不能变成被测模型的零分。`insufficient_data` 的值为 null，并说明缺了什么。每个指标可分别标记状态，例如有 token 但无价格时只缺费用结果。

批次不是一个巨大单次插件调用：run 评分按每条轨迹分配 job；comparison 评分按明确比较组分配 job。批次页汇总已完成、未评分和执行失败的数量。聚合必须带覆盖数与分母；先按 Case / 重复运行策略分组，避免运行次数多的题目无意增加权重。跨插件或跨版本默认不合并同名指标。

## 4. 插件访问平台的最小接口

下表是待实现接口提案，不是现有线上路由。HTTP 与 MCP 共用领域服务；SDK 只做薄封装。

| 能力 | 拟议 HTTP | MCP 对应能力 |
|---|---|---|
| 查看插件与版本 | `GET /api/plugins` | `platform_describe` 提供发现入口 |
| 创建批次或单次评测 | `POST /api/evaluations` | `evaluation_create`，仅向有触发权限的调用方开放 |
| 查看任务与固定配置 | `GET /api/evaluations/{id}` | `evaluation_get` |
| 认领与续租 | `POST /api/evaluations/{id}/claim`、`/heartbeat` | `evaluation_claim`、`evaluation_heartbeat` |
| 检查数据覆盖 | `GET /api/evaluations/{id}/coverage` | `trace_coverage` |
| 分页获取轨迹切片 | `POST /api/evaluations/{id}/query` | `trace_query`，只读查询 |
| 获取单条证据、产物、验收标准 | 任务作用域内的 record / artifact / case 读取接口 | `trace_record_get`、`artifact_read`、`case_get` |
| 提交一次 attempt 的结果 | `POST /api/evaluations/{id}/results` | `evaluation_submit` |
| 取消任务 | `POST /api/evaluations/{id}/cancel` | 调用方有取消权限才可使用 |

结果提交使用已有 `evaluation_result`，主要结构为：`job_id + attempt + plugin_ref + input_refs + config_digest + dependency_refs + scope + status + metrics + findings + execution_trace_ref`。待补齐的 job、查询、claim/heartbeat 和批次 Schema 应作为草案的下一版发布，不能仅靠这张接口表宣称契约已完成。

客户端凭据限定到授权项目、job 和固定输入范围。插件可以读取自己的评测输入、提交自己的结果，不能直连 PostgreSQL 或修改轨迹。比较插件可读取其比较组；验收答案需另有 `oracle_read` 权限。联网验收应把当次观察保存为新证据，标注观察时间与环境，不能把稍后的外部状态冒充任务结束时状态。

重复提交相同 attempt、相同内容返回同一结果；不同内容冲突。取消、租约过期后的结果拒收。认领和提交在数据库事务内处理，避免两个 worker 覆盖彼此。重试产生新 attempt，保留原错误。

## 5. PostgreSQL 怎么存

沿用现有事实存储，新增独立的插件及评测实体：

| 表 | 主要内容 |
|---|---|
| `plugin_versions` | 不可变 manifest、版本、声明摘要和实现包引用 |
| `evaluation_presets` | 集合 / Case 的默认插件与配置；与不可变导入清单分开 |
| `evaluation_batches` | 一次用户触发的固定成员清单和聚合策略 |
| `evaluation_jobs` | 一个插件作用于一份固定输入范围，配置与依赖快照 |
| `evaluation_attempts` | worker、租约、执行状态、重试、错误、评测自身用量 |
| `evaluation_results` | 每次 attempt 的不可变标准结果 JSONB 和摘要 |
| `metric_values` | 可重建的指标查询投影，支持列表排序、过滤与聚合 |

状态、身份和关联字段放结构化列，插件自定义内容放受约束的 JSONB；产物和执行日志使用内容引用。所有行按授权项目隔离。每个新插件复用这些表，无需创建专属评分表。首版任务队列可以由 PostgreSQL 事务认领支撑，暂不增加队列中间件。

## 6. 网页入口如何组织

沿用“功能入口 → 大列表 → 详情”的导航：

- 增加一个 **评测插件** 一级入口。列表查看可用插件、版本、执行方式及配置预设；插件详情看要求、指标和发布说明。插件名称不展开成一整棵侧栏树。
- **评测任务列表** 中选择集合后发起批量评测；集合内分为 Case 列表与评测记录。评测记录展示批次，进入后才逐项查看结果。
- **Case 页** 保留运行对比，加“运行评测”入口；不同插件的结果使用标签切换，不把所有报告同时展开。
- **运行详情** 展示指标、问题和证据。点击问题可定位某次调用、某一轮或产物；缺数据用“未评分：缺少终态产物”等直接说明。

首版平台统一渲染指标与 findings。后续如增加图表，使用受约束的声明式图表数据；不要求每个插件开发一份前端页面。当前草案未定义通用 report blocks，扩展时需同步更新结果 Schema 和验证器。

## 7. 首版落地顺序与验收

实施前的历史基线是固定的 `/api/runs/{id}/analysis` 及 `analyses` 缓存。此后评测插件注册、worker、HTTP/MCP 及结果页已上线，实际边界见本文开头的接入指南。以下保留原分步计划；通用 renderer/slicer 的接入遵循新的贡献点架构。

第一步落 Registry、Job、查询和标准结果闭环，用 **官方时间插件** 复用已有确定性计算。第二步接入 **任务验收插件**：程序检查业务产物，或 Skill agent 按 rubric 给出带证据的结果。第三步再迁移成本与轨迹诊断，并补齐批量评测及统一结果展示。LLM 自动评分需要明确的模型执行环境；外部 Skill 能够先验证接口，不冒充已有托管执行器。

完成标准：新增一种评分只需发布插件和声明，不改事实库表或主程序；同一输入能保存两个版本的结果；缺数据与任务失败可区分；证据可点击回原记录；取消和重试不会产生错误覆盖；插件自身消耗单独展示。首版不包含插件市场、在线任意代码托管和插件前端扩展。

## 参考

- [Inspect Scorers](https://inspect.aisi.org.uk/scorers.html)：评分方法与聚合指标分开，支持程序与模型评分。本方案采用统一结果及显式聚合口径。
- [Langfuse Scores via API/SDK](https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk)：外部应用或流水线可计算评分后写回，并关联 trace / observation / session。本方案采用外部评分、平台统一关联与展示。
- [Promptfoo Python assertions](https://www.promptfoo.dev/docs/configuration/expected-outputs/python/)：自定义程序输出结构化评分。本方案保留语言无关的外部执行边界。

参考于 2026-09-10；上述接口、表和实施顺序是 Trace Hunter 的工程提案。
