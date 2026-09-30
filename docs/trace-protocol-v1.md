# Trace JSON v1.1

机器定义：[`schemas/trace-v1.schema.json`](../schemas/trace-v1.schema.json)。完整可导入最小样例：[`examples/minimal.trace.json`](../examples/minimal.trace.json)。

## 输入顶层

| 字段 | 含义 |
|---|---|
| `schema_version` | 当前为 `trace-hunter/1.1`；旧 1.0 输入按原样兼容读取 |
| `run` | id、query_id、env_id、title、query、harness、model、status、time_basis |
| `environment` | isolation、network_access；可选观测时间、快照、工具版本、说明 |
| `collector` | 采集器名称与版本 |
| `sources` | 原始文件名、稳定来源 ID、SHA-256；不依赖某台机器的绝对路径 |
| `coverage` | tools / model_requests 的 complete、partial、missing 声明 |
| `phases` | ID、名称、task/setup/export、相对开始结束时间；可按阶段覆盖 coverage |
| `spans` | 实际调用与等待记录 |
| `links` | invokes / follows / retry_of 引用 |
| `evidence` | artifact、assertion、note，以及来源与相关 span ID |

禁止额外顶层 `totals`、`analysis`、`score`：这些由平台计算。自定义字段需先扩展协议，避免各流程私自漂移。v1 先支持单个 run 的完整 JSON 导入，后续再增加增量 NDJSON 封装；不把原始供应商 JSONL 直接当标准输入。

## 任务与环境身份

`run.query_id` 与 `run.env_id` 必填。相同 query_id 即可比较，不要求 env_id、harness 或 model 相同；不同 query_id 禁止混入同一对比请求。同一 query_id + env_id 可以有多次运行，由 run.id 区分。

下面是 1.1 输入中的身份与环境部分，完整 JSON 见最小样例：

```json
{
  "run": {
    "id": "run-003",
    "query_id": "supermarket-base",
    "env_id": "work-client-local-v1"
  },
  "environment": {
    "isolation": "non_sandbox",
    "network_access": "allowed",
    "observed_at": "2026-09-09T10:00:00+08:00",
    "snapshot_id": null,
    "tool_versions": {"lark-cli": null},
    "notes": "连接外部服务，工具返回及数据可能随时间变化。"
  }
}
```

`isolation` 必填：sandbox（沙箱）、non_sandbox（非沙箱）、unknown（未知）。`network_access` 必填：allowed、blocked、unknown；允许 sandbox + allowed 的组合。

`observed_at` 是当次环境观测时间，含时区的时间戳或 null；`tool_versions` 为工具名到版本字符串 / null 的映射，未采集可为 `{}`；`snapshot_id` 为可引用的环境或数据快照标识，无则 null；`notes` 保存环境限制或状态说明。这四项可省略。相同 env_id 不承诺联网服务和数据在不同时间完全相同。

旧 1.0 的 task_key 只在浏览投影中映射为 query_id；env_id 以 legacy 前缀区分，隔离与联网属性为未知。兼容投影不会修改输入或 hash。新采集请明确分配身份，不根据文本相似度或模型名称分配。

## Span 必填字段

`id, kind, name, operation, agent_id, parent_id, request_id, attempt, phase_id, start_ms, end_ms, duration_ms, status, input, output, usage, source`。

- kind 是 model/tool/wait/agent；operation 是 read/write/bash/skill/human/other，用于稳定可视化分类。Bash 即使执行了文件写入仍属于 Bash，业务副作用应单独建模。
- parent_id 表示运行时包含关系，不能出现环。model span 必须有 request_id。
- attempt 从 1 开始。同一次请求的消息分片可有不同 span.id，但 request_id、agent_id、attempt 相同。
- source 为 `{source_id, pointer}`；pointer 是原始文件内可复核的位置，如 `/tool_calls/12`。
- input/output 保留 JSON 或字符串；未捕获为 null。导入内容作为数据展示，不执行里面的命令或指令。
- 所有时间为相对同一起点的毫秒。只有端点时平台相减；只有 duration_ms 时可展示执行时长，但不能参与时间窗口区间并集。

## Usage 口径

```json
{
  "input_tokens": 1200,
  "output_tokens": 400,
  "cache_read_tokens": 1000,
  "cache_write_tokens": 0,
  "thinking_tokens": 100
}
```

此请求总量为 **1600**，不是 2700。缓存 1000 是输入 1200 的一部分，thinking 100 是输出 400 的一部分。

未知字段为 null，整个 usage 未采集也可为 null。usage 只允许挂在 model span。v1 只接收每个请求已确定的最终 usage 快照；中间流式累计值由 adapter 保留在原始文件，不转换成互相冲突的最终快照。

同一请求的重复快照完全一致时去重。若不一致，平台报告 `usage_conflict` 并将该请求排除出用量汇总，汇总明确标为部分记录，不猜测最后一个值或最大值。缓存用量可能反复读取相同上下文，它是请求工作量，不是独特内容长度，也不是未经加权的费用。

## 导入、浏览与后触发分析 API

完整字段、响应样例和错误约定见 [数据接口定义](api/README.md)，机器契约见 [OpenAPI](api/openapi.json)。

- `POST /api/import`：仅校验并存储标准 JSON，16 MB 上限。新导入 201，重复内容 200，run.id 冲突 409，非法字段 422；不触发分析。
- `GET /api/runs`：已导入记录及 query_id、env_id、环境属性。
- `GET /api/runs/{id}`：原始 `trace`、内容 hash、身份与环境元数据、调用展示 `view`；不含自动分析结果。
- `GET /api/runs/{id}?phases=["1","2"]`：指定展示阶段（query 值须 URL encode）。
- `GET /api/compare?id=...&id=...&scope=task`：1–6 次运行，query_id 必须相同；scope 可为 task / all；不触发分析。
- `POST /api/runs/{id}/analysis`：后续显式分析入口，生成或复用独立分析结果，只有点击“生成三项分析”才调用。可用 phases 查询参数选范围。
- `GET /api/schema`：当前机器协议。

调用 `view.rows` 通过 span_id 对应原始数据，含执行秒数所需时间值与 measured / interval_estimate / missing 标签。请求 token 汇总、瓶颈与验收结论只属于后触发的 analysis。响应将 cost / trajectory / time 分别返回；当前 trajectory 为技能与工具统计，LLM review 状态为 not_requested。

## 轨迹头部总量

`view.summary` 是只读展示事实，不触发评测或分析任务：

- `wall_ms`：当前所选阶段最早开始到最晚结束的跨度，包含中间等待与阶段间隔；任一阶段边界缺失即为 null。不能累加工具耗时替代。
- `user_turn_count`：实际用户对话轮数，按所选阶段统计；排除模型请求、工具返回、技能注入和已识别的重复记录。
- `user_turn_coverage`：complete / partial / missing；partial 的数量是已记录下限，页面显示“至少 N 轮”；missing 显示“未记录”，不补成 0。
- `user_turn_basis`：计数来源和限制，悬停可见。Case 的 conversation.turns 是题目输入定义，不是运行过程实际轮次。

当前 1.1 输入未定义完整用户消息序列。历史来源只通过已有白名单和 SHA-256 校验后做只读补充，不修改原始轨迹。Claude 导出排除 is_meta 消息，任务阶段统计为 2 轮；尚未完成的导出阶段按部分采集处理。Doubao 原生快照未声明全会话完整，轮数保留为下限。没有可验证来源的标准导入保持未知，后续完整消息/turn 协议另行接入。

两个总量跟随任务 / 全部阶段切换；工具切片器仅改变方格显示，不改变这两个总量。

## 颜色约定

完整色卡、每档秒数、配色主题和“显示类型字母”开关默认收起在“颜色说明”，点击或键盘展开。页面常驻说明“越深，耗时越长”，跟随调用 / 响应＋调用口径切换。

配色是视图设置，不进入轨迹协议，也不影响分析。默认仅用三种彩色标识主要工具类型：Read 蓝、Bash 绿、Write/Edit 橙。Skill、交互和其他调用共用中性灰，但保留各自类型与筛选入口，不合并或删除调用。所有色系以六档明度表示耗时，使用相同阈值与明显的明暗差异；可切换统一绿色或黑白。主题配置位于 `apps/trace-platform/theme.js`。字母默认隐藏，类型通过筛选、悬停和详情查看；无障碍标签始终保留类型与时长。开启字母后显示 R/W/B/S/?，读取 SKILL.md 的 Read 额外显示中性灰的小 S 标记。花纹不编码工具类型；错误用叹号与轮廓，缺失时间用灰色斜线与虚线。绿色与黑白主题下，筛选按钮不显示同色圆点。

切片器按 `operation` 多选，同层类型按“或”组合；技能标记与记录状态按“且”组合。Read 筛选包含技能加载，Skill 类型筛选指显式工具调用；“全部技能相关”可跨底层类型查看。过滤保留原始顺序与调用编号，只影响方格展示，不改变分析范围或结果。

技能标记单独展示：技能加载或显式 Skill 调用始终在方格左上角显示 S，不受“显示类型字母”开关影响。开启普通类型字母时，Read 可同时呈现 R 与 S，显式 Skill 只显示一个 S。标记不改变底层操作类型或颜色。

每种色系均为六档，跨任务使用相同阈值：100 / 1000 / 5000 / 15000 / 60000 ms。不能每条轨迹独立拉伸最大值，否则颜色不能比较。默认深浅表示调用用时，可切换为响应＋调用；后者可能含估算。

交互调用保留在方格与任务总耗时中，悬停显示“交互用时”。它记录整个调用区间，不能直接认定为纯用户等待。时间分析单列 `operation=human` 的 tool / wait 区间并集与非 human 工具区间并集，按所选窗口裁剪，缺失不补零。原 `tool_time_union_ms` 仍含交互工具调用，页面明确标为“调用占用（含交互调用）”；不将交互冒充自动工具执行，也不从总耗时机械扣减重叠区间。当前未提供排除纯用户等待的模型速度指标。

## 顺序与技能补充字段

1.1 的 span 可选字段新增：

- `sequence`：run 内统一的调用顺序，工具 sequence 不重复。时间缺失时仍按该序列进入方格。
- `order_basis`：timestamp / source_sequence / unknown，保存顺序依据，不替代执行时间。
- `skill`：`{name, action}`，action 为 load / invoke。加载 SKILL.md 保留原始工具名 Read；Skill 工具为显式调用。同一执行只产生一个方格。

有完整统一 sequence 时按它展示；没有 sequence 但全部开始时间已知时按开始时间排序；否则保留输入顺序并标注未核实，不把缺失值统一移到末尾。历史 Doubao 导出可依据 native 消息顺序和计时调用的锚点恢复相对位置；仅采用哈希匹配的原始来源。缺失时长仍是 null。

## 三类分析与价格

`POST /api/runs/{id}/analysis` 返回独立的 cost、trajectory、time。导入和 GET 浏览不触发分析。

成本先统计 token。有匹配 harness/model 的价格时，可估算输入、输出、缓存读取和缓存写入的分项费用。价格保存在平台配置而非原始轨迹，分析缓存键包含价格内容摘要。启动参数 `--pricing-file` 可加载价格 JSON，结构见 `config/prices.example.json`（仅合成测试价格）。价格无匹配、缺分项或用量缺失时，不以零费用代替未知。

当前价格适用于统一档位的模型 token 估算；混合模型、缓存 TTL、上下文阶梯、服务档位、外部工具费用和账单对账需单独扩展。没有运行大模型轨迹评审，不根据工具退出码判断任务质量。

多轮与压缩协议的下一版建议见 [研究文档](research/multi-turn-traces.md)，尚未纳入当前 1.1 的必填输入。
