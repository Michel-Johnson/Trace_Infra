# 豆包命令归档：逐字段映射与导入约定

适配器 1.3.0，目标协议 `trace-hunter/1.1`。当前入口支持 `builtin-traces/catalog.json` + `meta/items` 归档；它是命令观察记录的导出，不是完整模型事件流。机器可读定义在仓库 `contracts/imports/doubao-command-fields-v1.json`，每批 `source-audit.json` 根据同一份定义生成。

## 身份与层级

`conversationId → run`，`catalog.traces[i] → phase`，`items[j] → tool span`。同文件完全重复项保留别名映射；明确共享的子命令归入同一 span。原始 `messageIndex` 不直接变成用户轮次。一个会话可以有多个片段，但完整用户输入、模型请求、压缩事件需要其他原始证据，不能从片段数推算。

默认 `query_id` 是会话摘要身份占位，`env_id` 是该会话的未知环境占位。需要跨环境同题比较时，用 `--context` 提供题目与环境声明；不能按文本相似度或 CSV 分组自动匹配题目。

## 全部 53 个来源字段

下表的 `事实` 指每条 span 对应的 `evidence[id=record-facts-…].detail` 中 JSON；`片段身份`、`片段元数据` 也是 evidence note，分别引用原目录条目与 `/meta`。所有原文件另以原字节、SHA-256 和 JSON Pointer 保留。

### catalog 根对象（5）

| 字段 | 含义与标准落点 |
| --- | --- |
| `version` | 来源格式版本，只接受整数 1；记录在字段报告 `source_version`。不是标准协议版本。 |
| `generatedAt` | 导出生成时间，进入字段报告 `exported_at`；不是任务开始时间。 |
| `traceCount` | 必须等于目录条目数；对应全量包 `source_fragments`，不是会话数。 |
| `datasets` | CSV 来源组，完整保留到字段报告；不当作环境或模型分组。 |
| `traces` | 来源片段索引；按 conversationId 归组，位置保留到 session-index 与来源引用。 |

### datasets[]（8）

| 字段 | 含义与标准落点 |
| --- | --- |
| `id` | 来源组身份；原值保留，与条目 datasetId 对应。重复或引用不存在的组会阻止转换。 |
| `name` | 显示名称，保留在字段报告；不参与模型识别。 |
| `fileName` | 上游 CSV 文件名；不表示 CSV 已包含在归档中。 |
| `sourceBytes` | 上游声明的 CSV 大小；不与 JSON、压缩包字节数混用。 |
| `rowCount` | 上游 CSV 行数；与实际导出片段数的差额单独报告。 |
| `traceCount` | 上游声明的本组片段数；与目录按组计数核对。 |
| `commandCount` | 上游声明的原始命令项数；全组正文齐全时与 items 总数核对。 |
| `invalidRowCount` | 上游声明的无效行数；不能用它解释未说明的缺失行。 |

### catalog.traces[]（14）

| 字段 | 含义与标准落点 |
| --- | --- |
| `id` | 导出片段身份，保留在 session-index.fragments.id；不是 tool_call_id。 |
| `datasetId` | 来源 CSV 分组，保留原值；不是 env_id。 |
| `sourceRow` | 原 CSV 行号，正整数，从 1 开始；不是对话序号。 |
| `conversationId` | 按完整原值归组，保留在 session-index.conversation_id；不猜测它是否原生 ID 或摘要。 |
| `messageIndex` | 非负整数字符串，保留原字符串；参与无时间片段的顺序与身份校验，不等于 turn_index。 |
| `title` | run 标题及 Case 标题的展示预览；完整标题在片段身份与 raw。 |
| `promptSummary` | 明确标注“来源摘要”的 run.query 预览；完整摘要保留，不能充当完整用户 prompt。 |
| `responseSummary` | 片段回复摘要保留到片段身份；不是完整工具返回或质量验收。 |
| `startedAt` | 目录显示时间，保留并与 meta.startedAt 核对；实际排序使用 meta 与工具端点。 |
| `duration` | 人类可读耗时文字，完整保留；不解析为模型秒数。 |
| `commandCount` | 来源声明原始项数，与 meta/原始 items 核对；不替代去重后计数。 |
| `successCount` | 来源 success 标签计数；不是任务成功率。 |
| `failedCount` | 来源 failed 标签计数；与规范化后的 error_records 分开。 |
| `path` | 归档中的资源路径；只允许解析到已存在的受支持成员；映射为 sources.name。 |

### 每个片段文档（2）

| 字段 | 含义与标准落点 |
| --- | --- |
| `meta` | 来源片段元数据，完整写入独立证据；不当作整段会话元数据。 |
| `items` | 原始命令观察数组，组装 spans；原数组下标、重复项映射和共享关系可追溯。 |

### meta（8）

| 字段 | 含义与标准落点 |
| --- | --- |
| `title` | 目录没有标题时用作回退；完整原值保留在片段元数据。 |
| `startedAt` | 带时区的片段起点，换算为相对会话最早可见端点的毫秒；未知保持 null。 |
| `finishedAt` | 同上；窗口不足以包含已观测调用时扩展展示窗口，保留原窗口和修正原因。 |
| `duration` | 来源展示文字，只保留；不补算模型/思考时间。 |
| `commandCount` | 原始命令数组长度声明，字段报告核对原数组。 |
| `executionCount` | 导出方自有计数，语义未经独立证实时只保留；不直接用于标准调用次数。 |
| `successCount` | 原始 success 标签数；与原数组核对。 |
| `failedCount` | 原始 failed 标签数；与原数组核对。 |

### items[]（16）

| 字段 | 含义与标准落点 |
| --- | --- |
| `id` | 片段内局部 ID → 事实.source_item_id；跨片段不能只凭这个值删除重复。 |
| `seq` | 原始序号 → 事实.seq，非负整数；不同片段可能重置。 |
| `resultSeq` | 返回序号 → 事实.result_seq；只有 sharedDuration=true 且 seq/resultSeq 一致才支持合并共享执行。 |
| `subIndex` | 子命令索引 → 事实.sub_index；不是独立模型请求。 |
| `operation` | 原始操作标签 → 事实.source_operation 和 span 名称。来源是命令执行，标准工具类型始终 bash；业务类别由平台分类器后续判断。 |
| `description` | 原始描述 → 事实.source_description；不从描述推断成功与否。 |
| `status` | 原标签 → 事实.source_status；success/failed 映射 ok/error，其他标签保持 unknown 并提醒。显式进程或业务失败可以补记 error。 |
| `exitCode` | 进程退出码 → 事实.process_exit_code；null 不等于 0，0 不保证业务成功，非零明确失败。 |
| `durationMs` | 来源毫秒 → span.duration_ms；原值也保留在事实.source_timing。负值或端点矛盾时标准计时置 null 并说明。 |
| `sharedDuration` | 仅布尔 true 开启共享分组 → 事实.shared_duration；字符串 "true" 是格式错误。 |
| `startedAt` | 工具起点 → 相对毫秒 start_ms；保留原时间到 source_timing，缺失不补 0。 |
| `finishedAt` | 工具终点 → 相对毫秒 end_ms；与 durationMs 独立保留和交叉核对。 |
| `isHelp` | 导出方帮助命令提示 → 事实.source_is_help；不取代平台对真实 command 的识别。 |
| `command` | 单项写入 input.command；共享组写入 input.commands，保持每条完整原文与原顺序。导入时不执行。 |
| `result` | 完整 JSON 对象/数组仅解码一层放到 output；其他文本原样保留。共享组保留每个返回。原始字符串始终可从 raw 取回。 |
| `resultSummary` | 返回摘要 → 事实.source_result_summary；与完整 output 分开，不能拿摘要替代完整返回。 |

## 目标协议中的其他字段

| 目标字段 | 本来源的处理 |
| --- | --- |
| `run.id` | 转换器版本 + 原归档 SHA 前缀 + 完整 conversationId 的摘要；声明映射时加 context SHA 前缀。规则或声明改变产生新版本。 |
| `run.status` | partial，表示来源覆盖不完整；不能因为工具全部成功就声明任务完成。 |
| `run.harness` | Doubao Work，来自明确的输入格式。 |
| `run.model` | 默认“未知（来源未提供）”；只可由显式声明覆盖，不推测自动路由的实际模型。 |
| `collector.name/version` | 转换适配器身份与版本，不是原运行客户端版本。 |
| `environment` | isolation/network_access 默认 unknown，observed_at/snapshot_id 为 null，tool_versions 空；显式声明可覆盖。 |
| `coverage` | tools=partial，model_requests=missing；不能补齐不存在的事件。 |
| `span.id/phase_id` | 本 run 内稳定结构 ID，由片段排序位置与原 items 下标生成；原身份仍另存。 |
| `span.kind/operation` | tool/bash；读表、写表、help 都是该来源中的 CLI 操作，不伪装成 Read/Write/Skill。 |
| `span.sequence/order_basis` | 已知起点按时间排序，无时间记录保留在来源锚点附近；顺序修正有审计记录。 |
| `span.agent_id/attempt` | 协议适配默认 main/1，只是结构默认；无法从重复命令确认代理身份或重试次数。 |
| `span.request_id/parent_id/usage` | 无证据时 null；不虚构请求链，也不把上下文占用当累计 token。 |
| `links` | 未提供可证明关系时空数组。 |
| `evidence/source` | 每条事实指回原文件与 JSON Pointer，完整文件由 SHA-256 绑定。 |

不从两次工具之间的间隔推算模型生成时长；间隔可能同时包含思考、输出、排队、人等待、网络或遗漏事件。标准 time 字段统一为毫秒，页面展示秒由平台负责。

业务返回只在完整可解析的 CLI 响应（含 identity、error 对象或 data 字段）明确 `ok:false` 时作为业务失败证据。任意正文里的“error/失败”或数据记录里的 `ok` 不参与判断。每次状态修正保留原标签、进程结果、业务依据和规则版本。

## 声明题目和环境

使用仓库 `contracts/imports/doubao-import-context.example.json` 的结构。`sessions` 键必须是本批出现的完整 conversationId，query_id 必须引用同文件 cases，env_id 必须引用同文件 environments。只写已知字段，未映射会话继续使用占位。

```bash
python3 scripts/trace_import.py prepare --format doubao-command-corpus \
  --archive /absolute/source.tar.gz --output /absolute/new-bundle \
  --context /absolute/import-context.json
```

同一 query_id 的多个会话归到一个 Case，env_id 可以不同。Case.conversation 保存声明的题目轮次，**不表示来源实际观测到这些用户输入**。来源仍以 partial 覆盖保存，不生成模型 spans 或伪造轮次事件。声明原文件放在 raw/import-context.json，逐会话 evidence 标注 user_declared 并引用具体 sessions 条目。

## 字段检查、缺失与导入

`inspect --archive … --report …` 只在临时目录读来源，输出逐字段报告；不生成标准轨迹、不写平台。`prepare` 内置相同检查，不必重复跑 inspect。类型错误、缺少身份字段、非法时间/路径阻止转换；计数口径差异与新增未知字段报告后保留来源。可选字段缺失与显式 null 分开统计，不把 false、0、空字符串或缺失混为一谈。

标题等受协议长度限制的展示字段可以截短，但截短目标和长度必须记录，完整内容仍在来源证据。command/result 不截断；超过单文件 16 MiB 平台限制时明确失败，不能静默删内容。

完成包的 manifest 使用 `trace-hunter/import-bundle/1.0`，对 session-index、source-audit、样本 selection 分别保存字节数和 SHA-256。verify 核对原文件、标准文件、JSON 指针、目录身份、会话索引、实际记录数及原始项/重复项/共享项的计数守恒，并通过平台真实导入/读取路径验证幂等性、零分析触发。旧包仍可验证已有能力，但不会声称完成它没有提供的辅助清单校验。哈希用于完整性校验，不是来源签名或防篡改认证。

向平台提交 `manifest.import_files` 所列文件，顺序为 import/catalog.json 然后各 *.trace.json；保留导入响应供追踪。现有 `/api/import` 接收单份标准 JSON，网页可多选文件。raw、manifest、索引、字段报告不作为轨迹提交；本地完整包负责保留来源证据。当前离线入口不自动上传到在线平台，分析由平台另行触发。

样本保留入选会话在本批中的全部片段。为维持 JSON Pointer，原始 catalog 及提供时的 context 保持原字节，可能含未入选会话的摘要/声明；正文只复制入选来源。字段报告明确 catalog 全量范围与 selected 正文范围。修订版本时使用 `--selection-from` 保留相同 conversationId，不能只靠相同 seed。
