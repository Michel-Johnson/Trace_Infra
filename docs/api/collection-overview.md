# 批量分类与集合概览

集合页面以分页 Case 行为入口，不把整个集合的轨迹、工具入参和返回加载到浏览器。导入和 GET 请求始终不创建分类任务；用户显式运行所选分类插件后，再聚合它的不可变产物。

## 显式分类

`POST /api/collections/{collection_id}/classify`

```json
{
  "plugin_id": "official.base-stages",
  "plugin_version": "1.2.0",
  "contribution_id": "classify",
  "scope": "task",
  "request_key": "client-generated-stable-request-id"
}
```

使用插件的默认配置。返回 `collection_id`、`run_count`、`batch_ids` 和任务 `counts`。每块最多 50 个运行，按运行 ID 固定排序。`004_collection_classifications.sql` 保存整个分发请求的输入身份；相同请求标识重试复用已有块，中断后可继续，参数或输入变化则返回 409。重新分类使用新的请求标识。底层 `/api/plugin-runs` 仍支持单条/自选运行与自定义配置。

## 只读概览

`GET /api/collections/{collection_id}/overview`

- 插件选择：`plugin_id`、`plugin_version`、`contribution_id=classify`、`scope=task|all`。
- 分类筛选：`stage`、`intent`、`cli`。三个条件必须匹配同一工具记录。
- 状态筛选：`status=ok|error|unknown|running`。
- 对话筛选：`conversation=single_turn|multi_turn|unknown`，依据 Case 定义，实际轮次仍以可核对用户输入为准。
- 来源筛选：`fragments=single|multiple|unknown`，当前识别 Doubao command corpus 导入器的 `fragment-*` 来源。多片段不代表多轮。
- Case 搜索：`q` 搜索标题、query_id、query；`query_id` 为精确匹配。
- 排序：`sort=errors_desc|recent`，默认错误记录数降序；相同错误数按最近导入优先，再按 query_id 升序稳定排列。先对所有匹配 Case 排序，再分页。该顺序代表已观测错误数量，不是质量评分。
- 分页：`page=1`、`page_size=25`，每页最大 100；`pagination.sort` 返回当前排序。

响应类型见 OpenAPI `CollectionOverview`：

- `collection`：集合元数据。
- `summary`：整个集合统计；`filtered_summary`：符合筛选的 Case 的完整记录统计。`error_case_count` 是至少含一条失败工具记录的独立 query_id 数，不从当前页推断。
- `cases`：当前页行，包括名称、记录数、错误数/率、时长、用户轮次与覆盖、模型请求/token、来源片段数、阶段和操作意图。
- `cases[].display_id`：例如 `category 001`，按集合清单顺序编号；排序、筛选和分页不重新编号。`query_id` 继续作为导入、关联和比较的真实键。未分组视图按首次导入顺序编号。
- `cases[].display_description`：优先使用 Case 的 `description`，否则取首个输入的第一句话，再回退到标题；最长 240 字符。采集器的来源提示保留在完整输入中，不重复占用副标题。`q` 支持搜索编号和说明。
- `matched_tool_count`：该 Case 实际命中组合筛选的工具记录数量。Case 行总数仍保留完整运行上下文。
- `facets.stages / intents / clis`：整个集合中错误分布；排序按错误数降序，再按记录数降序。可点击值定位相关 Case。
- `filtered_facets`：与 `filtered_summary` 对应的完整 Case 记录分布，结构同 `facets`。精确 `query_id` 请求可直接获得当前 Case 的阶段/CLI 错误分布；不要拿全集合 `facets` 当作单 Case 结论。
- `tool_ms`：有时间记录的工具累计耗时，单位毫秒；`tool_time_known` 是已知记录数，`tool_time_coverage = tool_time_known / tool_count`。分类行 `tool_time_share` 以当前整个集合或筛选 Case 集合的已记录工具累计耗时为分母。
- `progress`：同一插件版本、默认配置、范围下每个运行最新任务状态。
- `pagination` 与 `notes`：分页信息和统计口径。

结果仅选取相同集合、插件版本、默认配置、范围、原始输入摘要的最新成功产物。重跑中仍展示之前成功的分类结果，同时单独显示最新队列进度。未知标签保留为 `__unknown__`，未运行分类保留为 `__unclassified__`。

## 统计口径

1. 工具数量是标准化记录数量，不是已证明独立的执行次数。错误率为 `error / (ok + error)`；未知和运行中记录各自列出，不进入分母。
2. 错误率定位调用记录的失败上下文，不自动证明 CLI 本身或远端服务有问题。可能是参数错误、解析代码错误、鉴权、网络或业务失败；原因诊断可以由后续插件提供。
3. 包含多个 CLI 的一条记录归入 `__multiple__`（多 CLI / 共享调用）桶一次。避免把同一失败硬归因到多个 CLI；精确 CLI 筛选仍可定位该共享记录。多选意图可以重叠，其各行计数不能再次相加为总体。
4. `base.intent=create` 表示可观测的创建 Base；`update` 表示更新 Base 内内容；`read` 表示读取/检查；缺少证据为未知。同 Case 可含创建和更新两类，不强制给整条轨迹贴互斥标签。
5. 用户轮次不由工具数、模型请求数、来源片段数或 Case 预设轮数替代。无法确认的模型请求数、用户轮次和 token 保持 null。已记录部分量带已知覆盖数量。
6. 模型请求按 `(agent_id, request_id, attempt)` 去重；冲突的 usage 不累计。标准 `input_tokens` 已包含缓存读写，`thinking_tokens` 是输出子集，不能重复加和。
7. 错误集中占比可以用某组错误数除以 `summary.error`（或筛选下 `filtered_summary.error`）；它与该组 `error / (ok + error)` 错误率不同。零错误时集中占比无定义，不能据此说“0% 风险”。
8. `tool_time_share` 不含模型和等待，也不推断未记录时间；某组全部时间缺失时 `tool_ms` 和占比为 null，已知零耗时则保留 0。分母为零时占比为 null。`facets` 和 `filtered_facets` 的分母分别来自 `summary.tool_ms` 和 `filtered_summary.tool_ms`。多选意图可重叠，各行耗时占比相加可能超过 100%，不能作为互斥饼图或相加为全量。
9. `wall_ms` 为已知运行所选阶段跨度的累计，包含阶段间空隙；不是一批任务共同运行的墙钟耗时。`wall_known_runs`、`tool_time_known` 显示可用计时覆盖。

## Case 分类历史分页

`GET /api/plugin-runs?collection_id=...&query_id=...&limit=100` 仍返回批次数组。默认 `limit=30` 保持兼容，最大为 100。将本页最后一项的 `id` 作为下一页 `cursor`，直到返回数量少于 `limit`：

```text
GET /api/plugin-runs?collection_id=...&query_id=...&limit=100&cursor=eval-...
```

顺序固定为 `created_at DESC, id DESC`。服务端使用不可变批次 ID 查找游标时间，再按时间和 ID 严格查询更早记录；读取过程中新增的较新批次不会使尾页偏移或重复。不存在或无效游标返回 422。兼容评分器 `/api/evaluations` 使用相同分页参数。

Case 客户端逐页读取全部相关批次，每页 100 条，支持取消和按 ID 去重；分页失败或游标不推进时报告错误，不以半份历史冒充最新结果。随后按插件版本、配置和范围，为每条运行选择最新成功产物；正在重跑时保留之前的成功结果。人工选择历史批次仍查看该次固定产物。整个读取过程不创建插件任务。

## 存储与规模

原始轨迹、插件版本和产物保持不可变。API 用输入摘要缓存紧凑记录事实，用产物身份缓存分类映射，不缓存大段工具输入/输出。切换筛选不反复解析完整轨迹。Case 详情按 query_id 查询任务和 PostgreSQL 运行索引，只读取该 Case 的运行/产物。首次概览仍需构建运行事实；当前是进程内缓存，服务重启后会重新构建。更大规模可以把同一投影持久化到带版本号的查询表，协议不变。
