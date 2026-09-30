# 精简后的评测接口

Core API 4.0.0。四方面评测共用六种取证/统计能力，共七个 HTTP 操作；工具选择与语义评分由评测 Agent 按评测规范完成。

项目接口前缀：`/api/v1/projects/{project_id}`。

| 方法与后缀 | 作用 | 对应分析 |
|---|---|---|
| POST `traces/query` | 选择样本，取得准确 revision 与摘要 | 四方面共用 |
| POST `objects/query` | 按字段读取对象、payload、来源与结构化信息 | 结果质量、过程、工具调用 |
| POST `spans/window` | 读取锚点前后步骤，展开正文预览、关联对象和已记录的边 | 执行过程、工具调用、错误恢复 |
| POST `search` | 在已选范围内搜索正文线索 | 按需补证据 |
| POST `metrics/query` | 调用量、耗时分布、错误率和已采集用量统计 | 效率与稳定性 |
| POST `evidence-exports` | 任务化导出固定选集的完整正文投影与对象证据 | 批量评测 |
| GET `evidence-exports/{task_id}/content` | 下载已完成证据文件 | 批量评测 |

查询 payload、导出正文仍受当前 scope 和授权约束。只判断模型当时信息时必须保持 model_context，不能回退到事后全量证据。缺失工具定义、历史 Skill 或验收产物时输出 unknown，不用当前版本补造历史证据。

## 保留的配套接口

| 功能 | 接口族 | 保留原因 |
|---|---|---|
| 版本和协议 | `/api/skills/manifest`、`/api/skills/archive`、`/api/openapi.json`、当前各 capabilities | 版本检查、下载与字段/权限发现 |
| 普通轨迹浏览 | projects、spans/query、traces/aggregate、revisions、content、sources/content | 前端轨迹库、工作台及原件审计 |
| 导入和采集 | traces 写入、imports、import-batches、uploads、import-adapters、/v1/traces | 接入新样本，保持自动导入工作流 |
| 任务管理 | tasks 创建/更新/读取/events/cancel/retry | 登记评测、展示进度、下载证据；不自动评分 |
| 网页 Agent | agent/sessions、messages、attachments、events、trace-labels、worker/* | 承载 Agent 会话和内部执行 |
| 数据格式及示例 | schema、trace-formats、examples | Adapter 开发、导入校验 |
| 运维和搜索索引 | health、observability、index、search/terms 及 promote/demote | 数据检查、索引恢复、已有热词索引管理；不执行评测 |

普通浏览接口不是额外的评测工具。评测 Skill 默认只推荐上述六种能力；配套操作按任务阶段使用。

## 已删除的 13 个公开操作

- GET `/api/v1/object-analysis-capabilities`
- POST `objects/analyze`、`objects/facets`、`objects/resolve-runs`、`objects/funnel`、`objects/lineage`
- POST `sessions/query`、`sessions/timeline`
- POST `spans/analyze-duration`
- POST `analysis-batches`
- POST `analysis-results/query`
- GET `analysis-results/{analyzer}/{analyzer_version}/{result_id}`
- GET `runs/{run_id}/revisions/{revision}/visibility`

这些路由从注册和机器合同中移除，正常授权请求返回 404；CLI 命令、前端操作和 Skill 指引同步移除。原本未接入 Core 的旧评测插件 HTTP 路由实现也删除。查询中的 model_context 权限和可见性限制继续保留。

原 Trace、历史结果和任务记录不删除，不做数据库迁移。历史分片分析任务可以回读或取消，但不能借 tasks/retry 重新启动已删除的计算入口；证据导出和自动导入仍按各自规则支持恢复。内部读取/投影算法保留，避免破坏证据模型。

## 调用顺序

确认版本和能力 → traces/query 固定样本及版本 → objects/query 或 evidence-exports 取证 → 必要时 window/search 补证据 → metrics/query 统计 → 评测器给出逐条结论和证据 → tasks 上报进度与产物。

完整的剩余路径和方法见 [接口清单](remaining-endpoints.md)，机器参数以 [OpenAPI](../../contracts/openapi.json) 为准。
