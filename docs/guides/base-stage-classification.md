# Base 阶段分类

插件：`official.base-stages@1.3.0`。读取标准轨迹，用确定性规则生成 `base.stage` 单选 facets；不调用模型、不执行轨迹里的命令、不修改原始记录。没有明确证据时保留 unknown。旧版本实现与结果保持不变，可回查或重试。

## 使用

1. 进入 Case，勾选需要比较的运行。
2. 分类工具栏选择「Base 阶段分类」。直接打开 Case 优先读取已有分类；要生成新结果，点击「运行分类」或「重新分类」。后台生成独立产物，完成后按阶段分组展示，未知调用仍保留。
3. 用「分类值筛选」选择阶段；「显示顺序」可恢复时间/原始顺序，也可按工具、前置响应或响应＋工具用时降序排列。原始调用编号不变，点击仍进入同一条来源记录。
4. 「更多 → 步骤统计」查看每条运行的阶段时间、用户轮次和 token；「查看当前产物」查看逐条分类依据。「分类结果与记录」集中选择分类器版本和历史产物；「高级运行」设置参数及外部能力。主栏每个分类能力只保留一个入口，版本不会混算。

一次分类绑定所选运行摘要和 task/all 范围。打开页面、切换视图、筛选和排序不发起模型调用或后台分析。时间统计是已存在轨迹和分类产物的即时汇总，当前不另存为计算产物。

集合页提供「Case 列表 / 整体分析」页签，`?view=analysis` 可直接进入整体页。整体页统计所选插件和 task/all 范围下的全部 Case，使用 `overview.summary / facets`，不使用列表筛选结果或当前页的 Case。阶段、CLI 和 Create/Update 分布可下钻回列表；点击失败数字会同时筛选失败记录。未知 Token、用户轮次和计时保留未知，多意图及跨阶段 Case 不累加为独立 Case 总数。

## 阶段和依据

| 阶段 | 主要依据 |
|---|---|
| Plan | host_plan_agent、host_create_plan_agent；无领域上下文时的 create_plan/read_plan/update_plan |
| Spec | spec_lite/standard/pro、requirements_generator、data_table_designer、workflow_designer 及相应 editor |
| Table | table、table_planner、agentic_table_agent；Base table/field/record/view 命令 |
| Flow | workflow、workflow_adapter_agent；Base workflow 命令 |
| Dashboard / Form / Permission / App | 对应领域 Agent 或 CLI 模块 |
| 准备 | Base 参考文档、技能加载路径、认证检查及资源定位 |
| 验证 / 交付 | 明确的 test/critic Agent、完成或产物交付工具 |
| 未知 | 无标识、混合模块命令或无法可靠判断的记录 |

Agent 归属优先于通用工具名；最近父级 Agent 优先于外层 Agent。Table 和 Workflow 的内部规划也使用 `create_plan`，不能全部归入顶层 Plan。`workflow_designer` 是 Spec 设计，不等同于 Flow 执行。参考文档正文和任意自然语言不作为已经执行了某阶段的证据。

模型、工具、等待与 Agent span 都可以分类。原本未知的模型请求，只有当它关联的工具全部属于同一个已知阶段时才继承该阶段，并记录工具证据。没有工具的 Plan/Spec 模型调用依靠自身 Agent/阶段记录分类。第一版未做自然语言语义分类、连续阶段边界推断、代码文件展开或任意 SDK 代码识别；这些缺口显示为未知，不推算补齐。

规则依据来自用户指定的 `backend-core` 和 `lark-cli`：

- 本地 backend-core 参考 HEAD：`2a68dc05b7545ac1bbbb25077c97e1fe84399d1b`；重点读取 `application/appservice/ai_ware/agent/constants/agent_names.go`、`structs/structs.go`、`host_agent/host_agent.go`、`table_agent/table_agent.go`、`workflow_adapter_agent/workflow_adapter_agent.go` 和 `spec_agent/common/document_tools.go`。
- 本机 `lark-base` 技能 1.2.21 的资源模型和 `lark-cli base` 命令模块；CLI 执行命令按 shell token 解析，不用正文里出现的 table/flow 词语分类。
- 既有 `building_agent_e2e_analysis/analyze_agent_task.py` 用于核对历史 SPEC_AGENT / TABLE_AGENT / WORKFLOW_ADAPTER_AGENT 标识。平台没有复制原始业务轨迹或这些项目的完整源码。

## 时间口径

- 总时长沿用所选运行范围的首尾跨度；不把分类耗时之和当作墙钟时间。
- 工具列累加各工具已记录的执行时长，并显示有计时的数量。交互型工具与 wait span 单列，Agent 容器跨度不再重复累加。
- 模型按 `(agent_id, request_id, attempt)` 去重；同请求计时快照冲突时保留未知，不任选一个。跨分类请求单列，避免多阶段重复累计。无 request_id 时按 span ID 区分。
- 前置响应间隔可能包含调度、网络等，只供逐调用查看与排序，不进入“模型累计用时”。缺失不记作零。
- 时间表覆盖当前选择的运行与 task/all 范围，不受工具类型按钮筛选影响；未生成分类的运行保留为未知。并发记录可能重叠，累计用时只描述工作量。
- 步骤「耗时占比」以当前运行各分类的已记录累计时间（工具、去重模型请求、等待）之和为分母，包含未知分类及跨分类共享请求。它不是墙钟时间占比；缺失计时不补零，确实记录为零的步骤在正分母下显示 0%，全零或未记录时不计算占比。批量页单独标注「已记录工具耗时占比」，只使用工具计时。

## 实现

[Manifest](../../plugins/extensions/base-stages-1.3.0/manifest.json) · [分类器](../../plugins/extensions/base-stages-1.3.0/classify.py) · [可复现包构建](../../scripts/build_base_stages.py) · [排序与统计](../../apps/web/src/plugins/classification-view.ts)

使用已有插件任务表与 worker，无新增数据库迁移、依赖或模型配置。模型和等待分类通过稳定 Manifest 的命名空间扩展声明；详见[通用插件接入](general-plugins.md)。更新分类规则须发布新的插件版本，不覆盖已注册版本的包摘要。


## 1.1.0：流程顺序与步骤统计

插件固定声明以下展示顺序：**Plan → Spec → Skill / CLI 读取与准备 → Table → Form → Dashboard → Workflow → Permission**；App、验证、交付和未知跟在后面。每组内保留原始调用顺序、编号以及缺少时间的记录。读取 Skill / CLI 参考资料是独立准备步骤，即使发生在领域 Agent 内也不会混入该领域执行。实际依赖和并发由轨迹记录，不因展示排序而改变。

每个步骤显示时间、用户轮次、已记录模型请求数和 token。只有模型调用、没有工具调用的步骤也会展示。统计覆盖完整步骤，不受工具类型筛选影响；抽屉可展开查看缓存和 thinking 明细。

- Token 使用 `(agent_id, request_id, attempt)` 去重。相同 usage 快照只计一次；冲突快照不计入，并显示冲突数；部分数据标记为「已记录」。
- 总 token 仅累加同一请求完整的输入/输出对。缓存读取和写入是输入子集，thinking 是输出子集，不能再加一次。没有使用上下文占用量代替 token 消耗。
- 用户轮次来自校验摘要后的源会话消息位置，排除工具返回、技能注入和压缩摘要。API 的只读 `view.span_turns` 给出逐 span 的关联；没有来源映射时显示未记录，不按 Case 或调用数猜测。
- 同一用户轮次可能参与多个步骤，因此各步骤轮次不可相加。序号表示当前采集内的用户消息顺序；不宣称是缺失前文时的全会话序号。
- 现有 v1.0 / v1.1 输入缺少逐 span 用户轮次的通用字段；这个版本保留原协议和数据，不添加私有伪字段。完整通用会话导入仍需接入后续会话协议。
- 成本暂不显示金额；后续按输入、输出、缓存价格及其币种、版本计算。

1.0.0 与 1.0.1 的包内容和历史结果不改写。重新点击一键分类时默认使用 1.1.0，旧产物仍按自身声明顺序呈现。


## 1.2.0：命令语料与批量分析

按标准 `operation=bash` 解析实际输入，支持来源工具名 `lark-cli +...`、JSON 命令对象和显式 `shared_execution` 命令数组。标题、描述、工具返回里的示例命令不作为实际执行证据。帮助命令独立归准备/读取，不计作创建 Base。跨领域混合命令保持未知阶段。

新增 `base.intent` 与 `base.cli` 两个多选 facet。intent 区分实际创建 Base 动作、更新内容动作与读取/检查；同一 Case 可以兼有多种操作。CLI 值仅含命令模块、动作和 help 标记，不含参数。一个共享执行只有一条标准记录；多 CLI 记录在错误排行中单列，不重复归因。通用 facets 每维最多 100 个标签，单条轨迹超限命令明确保留未知。

集合入口可以显式对全部运行创建分类任务，每批最多 50，按版本/配置/范围/输入摘要读取结果，分页展示错误分布和 Case 摘要。详见[批量接口](../api/collection-overview.md)和[50 样本检查](../reports/corpus-development-20260911.md)。旧版插件内容和历史产物保持原样。

## 问题优先的浏览

集合默认先展示失败记录最多的 Case，可切换最近导入。整批提醒与 Case 提醒分别标注统计范围，点击阶段或 CLI 定位同一条记录；未知分类、未分类和多 CLI 共享调用也能下钻。这个顺序是错误计数排序，不是插件质量评分。

Case 的「最新有效结果」按插件版本、配置和范围合并各运行最新的成功分类，支持同一 Case 的运行分散在多个执行批次。重跑期间继续显示此前有效产物；历史批次仍可单独选择和检查。没有有效产物的运行保留未分类。

这些排序、提醒和时间占比由平台读取原始记录及标准 facets 后展示，不修改分类器或已发布包。插件目录按身份聚合卡片，完整定义及历史版本在详情查看；导入、浏览和排序均不创建分析任务。

## 1.3.0：Shell 包装与组合调用

识别明确的 PowerShell / pwsh `-Command`、cmd `/c`、bash/sh `-c/-lc` 字面命令体，以及环境变量前缀、PowerShell 结果赋值、循环内的 `do`、`xargs` 转交命令和换行续接。解析保留引号与语法边界，只读取实际命令位置；不执行脚本、不展开变量、不扫描输出或 Python 打印内容。引用的脚本文件、动态命令、函数定义与 here-document 正文不作为已执行 CLI 的依据。

同一标准记录中所有可识别 CLI 分属多个模块时，归入单值 `mixed`（跨模块 · 组合调用），理由列出模块，CLI facet 保留全部动作。记录数、错误数和耗时仍只计一次，不把一条调用复制到各模块。部分命令未识别时仍保留未知，防止帮助查询等已知部分掩盖未知动作。

`+base-block-create --type docx/document` 归入 `document`（内嵌文档）。补齐 `bind`、`submit`、`upload`、`arrange` 写操作意图。参数值不进入分类标签；拼写不完整的 `base record-get` 不擅自改成 `base +record-get`。未知和未运行分类插件仍分别保留。

这是新增的不可变插件版本。已有导入数据可直接运行新分类，不必改写原始输入；历史 1.2.0 结果可继续查看。
