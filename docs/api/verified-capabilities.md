# 当前接口能力与历史验收记录

当前接口以 [3.0.0 保留清单](remaining-endpoints.md)和[评测入口](evaluation-surface.md)为准。以下内容是截至 2026-09-24 的历史验收记录，包含现已移除的专用分析接口，不是当前调用指南。

## 2026-09-24 历史基线

本文只记录截至 2026-09-24 已经通过自动化测试或本地规模验收的能力。机器可读字段、请求约束和响应 Schema 以 [`contracts/openapi.json`](../../contracts/openapi.json) 为准；本文说明 Agent 和用户可以用这些接口完成什么任务，以及证据边界。

## 1. 验证基线

| 验证层 | 结果 | 覆盖 |
|---|---:|---|
| Python 全量回归 | 532 项：431 通过，101 项因环境条件跳过 | API、CLI、权限、不可变存储、查询、搜索、Adapter、任务、服务端查询缓存、PostgreSQL；跳过项均有环境条件 |
| PostgreSQL 专项 | 通过 | 8 张核心表及 2 张可重建热词辅助表、迁移幂等、项目隔离、时间与 JSONB GIN 查询计划 |
| 前端生产构建 | 73 项测试通过 | OpenAPI 类型、搜索、导入、任务、分析工作台；HTTP 环境 SHA-256 回退；跨路由查询缓存 |
| 长 Trace 查询 | 5000 Trace / 500000 Span | Metrics、游标、时间索引、100 个可恢复批次分片 |
| 大文件导入 | 85,983,232 字节 / 11 分块 | 续传、分块摘要、总摘要、Adapter、存储、v4 投影 |
| OTLP | JSON、protobuf、gRPC 均通过 | Resource/Span attributes、状态、时间、事件、links、Session、幂等 |

规模数字是本机回归基线，不是生产 SLA。测试只使用代码生成的合成数据；真实 Trace、benchmark 原件和数据库文件不进入仓库。

## 2. 项目、原件与 Revision

| 接口 | 支持的场景 | 已验证行为 |
|---|---|---|
| `POST /api/v1/projects` | 创建隔离的数据空间 | 相同项目身份稳定；跨项目查询不泄漏 |
| `GET /api/v1/projects` | Agent 发现可用项目 | 游标式项目列表 |
| `GET /api/v1/projects/{project_id}` | 确认目标项目 | 不存在项目返回 404 |
| `POST .../traces` | 直接导入已符合协议的 Trace | 原始字节不可变、幂等键、防并发分叉、写入后投影 |
| `GET .../revisions` | 查看一条 Trace 的修订历史 | 明确 revision，倒序分页，不把“最新”猜成指定版本 |
| `GET .../revisions/{revision}` | 读取修订元数据 | 返回 digest、格式、来源和投影状态 |
| `GET .../content` | 审计不可变原件 | 校验 SHA-256，内容损坏明确失败 |
| `POST .../index` | 显式重建可恢复投影 | 不改原件、不新增 revision；失败可再次重建 |

可实现的任务包括：确认某次分析使用了哪一版 Trace、审计 Adapter 输出、验证重复导入是否产生副本、在投影损坏后恢复索引。

## 3. Adapter、分块上传与 OTLP 采集

| 接口 | 支持的场景 | 已验证行为 |
|---|---|---|
| `GET /api/v1/adapter-import-capabilities` | 发现最新目标 Schema、Adapter 和大小上限 | 返回直传与可续传上限 |
| `POST .../imports` | 小于等于 16 MiB 的已注册格式导入 | 六阶段进度：读取、转换、校验、存储、对象投影、Trigram 就绪 |
| `GET .../imports/{job_id}` | 查询单次 Adapter 任务 | 返回阶段、错误、结果和来源摘要 |
| `GET .../import-batches/{batch_id}` | 查看批量文件导入 | 数量、成功/失败、吞吐、ETA 和每文件阶段 |
| `POST .../imports/uploads` | 创建大文件续传会话 | 文件大小、总 SHA-256、分块大小和幂等身份固定 |
| `PUT .../parts/{position}` | 并发或断点上传 | 每块摘要校验；重复相同块幂等，不同内容冲突 |
| `GET .../uploads/{upload_id}` | 恢复中断上传 | 返回已收和缺失分块、字节数及进度 |
| `POST .../complete` | 完成上传并启动 Adapter | 校验总大小/摘要；分块进入内容寻址存储；不截断正文 |
| `DELETE .../uploads/{upload_id}` | 删除未完成上传状态 | 已完成上传不可误删 |
| `POST /v1/traces` | OTLP/HTTP JSON 或 protobuf 采集 | 通过 `X-Trace-Project` 指定项目；不自动分析 |
| gRPC `TraceService.Export` | Collector 实时批量上报 | 服务随 API 启动；使用 `x-trace-project` metadata |

OTLP 映射保留 Resource/Span attributes、绝对时间、状态消息、事件、links、scope 和 Session。跨 Trace link 无法成为本地对象边时，仍保存在 Span attributes 中，不静默丢失。

## 4. Trace 与 Span 查询

| 接口 | 支持的场景 | 已验证行为 |
|---|---|---|
| `GET /api/v1/query-capabilities` | Agent 发现 Trace 查询字段 | 不读取正文、不触发分析 |
| `POST .../traces/query` | 按 run、query、env、模型、状态等筛选 Trace | latest/all、字段选择、稳定游标、项目隔离 |
| `POST .../traces/aggregate` | 汇总 Trace 元数据与投影覆盖 | 已知、未知和空集合分开，不把未知算成 0 |
| `GET /api/v1/span-query-capabilities` | 发现 Span 字段和限制 | 返回当前 projector 版本 |
| `POST .../spans/query` | 按 Skill、Tool、状态、耗时、顺序查 Span | 精确过滤、时长排序、游标、revision 证据 |
| `POST .../spans/window` | 从锚点向前/后取最多 100 个 Span | 来源顺序连续；可展开正文、对象与边；不宣称因果 |
| `POST .../spans/analyze-duration` | 查 Skill/Tool 耗时异常 | 固定 revision 证据；样本不足明确返回 |

可实现的任务包括：查看 Skill 调用前 50 个和后 20 个步骤、定位一条 Trace 最慢 Tool、比较某 Skill 的耗时分布、读取锚点关联的 Context/Message/ToolCall。

## 5. 正文搜索

| 接口 | 支持的场景 | 已验证行为 |
|---|---|---|
| `GET /api/v1/trace-search-capabilities` | 发现搜索模式、字段、对象类型与后端 | PostgreSQL 使用 `pg_trgm` 加速正文候选召回；SQLite 仅测试回退 |
| `POST .../search` | literal、受限 regex 搜索 | 返回命中片段、字符范围、截断状态、score 和 source refs |
| `GET .../visibility` | 审计模型实际可见内容 | 缺失可见性证据保持 unknown |

适用场景：查命令、报错、文档名、Skill 名称附近正文；组合 Skill/Tool/status 结构化过滤；从命中文本跳回 Trace 和 Span。literal 是精确子串语义，regex 用于受限模式匹配。

PostgreSQL 搜索把精确总数与当前页建立在同一个物化匹配集合上，正文只在页内 ID 确定后读取。`benchmark-a` 上高频词 `tool` 的 33,914 个命中、1,889 条 Trace 保持不变，冷查询由重复扫描约 4.3 秒降至单次扫描约 2.13 秒；重复相同请求继续由服务端查询缓存处理。

对命中不超过 50,000 份文档的字面量查询，服务端还会有界缓存已排序的匹配文档键 5 分钟，按项目数据版本自动隔离和失效；换页无需再次扫描正文或重算总数。`benchmark-a` 的 `tool` 查询在独立进程里实测首查约 2.14 秒，随后两页约 13 毫秒和 5 毫秒。首查仍需精确核对正文，不宣称已消除冷查询成本。

评测检索有独立的可选词库：CLI 的 `search --evaluation` 仅统计首次请求中的事后分析 literal 查询（含总命中数、次数、平均耗时）；`search-terms` 展示候选，`search-term-promote` / `search-term-demote` 人工维护每项目最多 16 个活跃词。活跃词走可重建的精确子串倒排表，非活跃词仍走原 PostgreSQL 搜索；普通搜索、翻页、regex 和模型可见性查询不写候选统计。倒排在投影事务中同步，重投影先移除旧条目再重建，原始 Trace 不变。CLI 是用户入口，API 只提供其传输协议，不新增前端页面。

## 6. 通用对象查询、Facet 与固定快照

| 接口 | 支持的场景 | 已验证行为 |
|---|---|---|
| `GET /api/v1/advanced-query-capabilities` | Agent 发现字段、排序、Metrics 和预算 | 返回 `trace-index/4` 能力 |
| `POST .../objects/query` | 查询 Span、Message、Context、ToolCall | 绝对时间、任意 object/trace attribute、字段选择、排序、游标 |
| `POST .../objects/facets` | 先发现真实字段值再构造查询 | 固定字段和 `attributes.<key>`；缺失桶、limit、truncated |

快照由 `watermark + projector_version + query_digest` 绑定。查询只读取快照时间前已经完成的投影；分析运行期间的新导入或新投影不会改变当前选集。PostgreSQL 属性等值查询可使用 JSONB GIN，绝对时间范围可使用范围索引。

适用场景：先发现实际 Skill/模型/环境值，再查询对象；按版本、区域或自定义采集属性分组；只取 Agent 需要的字段，避免下载整条 Trace。

## 7. Metrics、Cohort 与批量漏斗

| 接口 | 支持的场景 | 已验证行为 |
|---|---|---|
| `POST .../metrics/query` | count、rate、错误率、sum/avg、P50/P95/P99、直方图 | 时间桶、最多五维分组、exemplar、known/unknown |
| `POST .../objects/resolve-runs` | 生成有/无某行为的 Trace 集合 | positive/negative 集合及覆盖证据 |
| `POST .../objects/funnel` | 统计 A→B→C 完成率与中断位置 | 按来源顺序执行，不伪装成因果 |
| `POST .../objects/analyze` | 组合对象过滤、关系步骤与聚合 | 受预算限制、可保存不可变结果 |

适用场景：Skill/Tool P50/P95/P99、错误率和 Token 覆盖；使用/未使用 Skill 的 Cohort；5000 Trace 的行为漏斗；按模型、版本、环境和用户群发现慢请求聚集维度。

状态缺失时，错误率为 unknown，而不是 0%。耗时缺失的对象不进入耗时分位数，响应同时给出覆盖情况。

## 8. 递归关系与 Session

| 接口 | 支持的场景 | 已验证行为 |
|---|---|---|
| `POST .../objects/lineage` | 查询祖先、后继、依赖路径和子图 | 不限四步；使用节点和字节预算；超限返回 partial warning |
| `POST .../sessions/query` | 发现跨 Trace Session | namespace/id、对象数、Trace 数、起止时间、exemplar |
| `POST .../sessions/timeline` | 按绝对时间回放会话 | Span 以及挂载的 Message/Context/ToolCall；返回 source refs |

适用场景：追踪 proposal→execution、retry、父子 Span、Context→Message；回放一个 Agent 会话跨多个 Trace 的执行过程。关系边表示来源中明确记录的关系；Span 来源顺序本身不等于因果。

## 9. 证据导出与批量分析

| 接口 | 支持的场景 | 已验证行为 |
|---|---|---|
| `POST .../evidence-exports` | 固定选集批量获取分析证据 | NDJSON，包含 manifest、对象、正文投影、summary；不逐条下载 Trace |
| `GET .../evidence-exports/{task_id}/content` | 一次下载已完成证据 | 内容寻址引用、摘要和 no-store |
| `POST .../analysis-batches` | 超过 5000 Trace / 50000 对象的分析 | 自动按 run 分片、固定快照、精确合并、可取消 |
| `POST .../tasks/{task_id}/retry` | 恢复失败的批量分析或证据导出 | 已完成分片引用保留，未完成分片重新排队 |

5000 Trace / 500000 Span 已验证拆成 100 个分片并精确返回 500000。分片中间结果进入内容寻址存储，因此服务重启后的显式 retry 可以复用完成分片。

## 10. 任务进度与前端同步

| 接口 | 支持的场景 | 已验证行为 |
|---|---|---|
| `GET /api/v1/task-capabilities` | Agent 发现状态协议 | poll + SSE、可重试任务来源 |
| `POST .../tasks` | Agent 注册自定义评测/分析任务 | 幂等 request key、阶段定义 |
| `PATCH .../tasks/{task_id}` | Agent 发布处理进度 | 单调进度、processed/total、事件、结果、错误、产物 |
| `GET .../tasks` / `GET .../{task_id}` | 前端读取统一状态 | 项目隔离、稳定游标分页、保留上限、服务重启后回读 |
| `GET .../{task_id}/events` | 实时订阅状态 | SSE revision，终态结束 |
| `POST .../cancel` | 取消可安全取消的任务 | 终态不可反向修改 |

任务接口支持按 `next_cursor` 稳定续页，当前持久保留上限为 1000；前端“任务进度”页读取最近 200 条任务，展示 Adapter、评测、分析和自定义任务。批量导入总量以 `import-batches` 汇总为准，不再由最近任务条数推断。失败的 analysis batch/evidence export 可直接重试，证据产物可下载。Agent 若绕过公开 API，在自己进程中直接调用 Python 类，前端不会看到该过程；这不是受支持的用户调用方式。

## 11. 数据质量、结果与可观测性

| 接口 | 支持的场景 | 已验证行为 |
|---|---|---|
| `GET .../observability` | 检查存储与投影健康 | Trace/Object/Search/Edge 数量、Timing/Status/Context/Token 覆盖、Trigram 状态 |
| `POST .../analysis-results/query` | 查已保存分析结果 | 精确 analyzer/version/result 身份 |
| `GET .../analysis-results/...` | 审计不可变分析产物 | 输入 digest、固定 revision 和来源证据 |

可识别的数据问题包括：正文截断、状态未知、Context/Token/Timing 缺失、投影失败、内容摘要损坏和采集覆盖不足。接口只报告可证明的信息，不从聊天历史反推模型 Context。

## 12. 权限与防信息穿越

- `analysis` 查询需要项目读权限与分析搜索权限；`model_context` 只允许查询采集时证明模型可见的对象。
- model-context 查询绑定明确的 run、revision 和 model span，不跟随当前最新 revision。
- 后生成的模型输出、未来 Message、缺失 availability 的对象不会进入先前模型的 Context。
- 项目 ID、snapshot、cursor 和 query digest 相互绑定，不能跨项目或换条件复用。
- 导入、浏览、搜索和投影不会隐式触发评测或分析。

## 13. 前端可检验入口

| 页面 | 可检验内容 |
|---|---|
| `#/search` | literal/regex 命中、片段、候选数、耗时与 Trace 跳转 |
| `#/traces` | Trace 列表、Span 时间线、状态、耗时和 source refs |
| `#/imports` | 小文件直传、大文件分块进度、六阶段 Adapter 进度、批量吞吐 |
| `#/tasks` | Agent/CLI/导入/批量分析统一状态、取消、重试和证据下载 |
| `#/analysis` | 对象、Metrics、lineage、Session、证据导出和批量分析的真实 JSON 请求/响应 |
| `#/health` | PostgreSQL、Trigram、对象数量和采集覆盖 |

前端对项目列表、Trace 列表、不可变 revision 正文、搜索结果、任务摘要和数据质量报告使用有界内存缓存。返回页面时先显示缓存结果，再按各自 TTL 后台刷新；后台刷新不会清空已有内容。网页导入成功后按项目失效 Trace、Span、搜索与质量缓存，手动刷新可强制绕过 TTL。

服务端对只读项目查询提供有界结果缓存，缓存键包含权限身份、请求参数和项目数据版本；浏览器刷新后仍可命中，新增 revision 或投影完成后自动失效。响应头 `X-Trace-Hunter-Cache` 标识 `HIT/MISS/BYPASS/SKIP`，`X-Trace-Hunter-Data-Version` 标识当前项目数据版本。

## 14. 明确边界

- 当前不是分布式 OLAP 系统；500000 Span 已通过，但更大规模仍需按项目容量规划 PostgreSQL。
- Session 依赖来源提供稳定 session id 和可靠绝对时间；缺失时不会猜测。
- 错误率依赖明确状态，unknown 不进入成功/失败分母。
- lineage 只遍历已存边，不从相邻 Span 推断依赖。
- 正文证据使用 literal、regex 或结构化字段；当前不提供近似搜索。
- 大文件上限当前为 256 MiB；超过上限应拆成来源语义独立的 Trace，而不是截断正文。
