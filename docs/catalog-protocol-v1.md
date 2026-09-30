# 集合与 Case 清单协议

`trace-hunter/catalog/1.0` 定义评测集或任务集及其中的 Case。它与 `trace-hunter/1.1` 运行轨迹分别导入，不修改原始轨迹或分析结果。

## 三层身份

| 层级 | 身份 | 含义 |
|---|---|---|
| 集合 | `collection.id` | 组织一组 Case，`kind` 为 `evaluation`（评测集）或 `task`（任务集） |
| Case | `query_id` | 任务及输入定义；可以属于多个集合 |
| 运行 | `run.id` | 某个 harness、模型、env_id 对该 Case 的一次执行 |

`query_id` 仍是全局对比身份。同一 query_id 的运行可比较，不因集合、模型、环境不同而被拒绝。集合不会成为新的对比门槛，也不复制其成员 Case 的轨迹。同一 Case 出现在多个集合时，各集合可引用同一批运行；跨集合累计时不能重复累加。

```json
{
  "schema_version": "trace-hunter/catalog/1.0",
  "collection": {
    "id": "ledger-suite-v1",
    "title": "进货台账评测",
    "kind": "evaluation",
    "description": "比较多个 harness 和模型"
  },
  "cases": [{
    "query_id": "ledger-multiturn-v1",
    "title": "分两轮建立进货与付款台账",
    "conversation": {
      "mode": "multi_turn",
      "turns": [
        {"id": "input-1", "prompt": "建立进货台账。"},
        {"id": "input-2", "prompt": "在刚才的台账上增加付款记录。"}
      ]
    }
  }]
}
```

## 单轮与多轮

`conversation.turns` 是按顺序提供的用户输入定义。`single_turn` 必须恰好一轮，`multi_turn` 至少两轮，`unknown` 必须为空；每个输入都有唯一 ID。改变任务目标或固定多轮输入时应使用新的 query_id。

Case 定义与实际运行对话分别处理：一个单轮任务可能在某个 harness 中发生用户澄清、多次模型请求或上下文压缩。这不会自动把其 Case 改成多轮。实际用户轮次、模型请求、压缩续接是后续运行事件协议的职责；当前清单不伪造这些事件，也不把工具调用数量视为对话轮数。

当前多轮支持固定的顺序输入清单及展示。它不是自动执行器，不包含动态用户模拟器或根据前一轮答案分支的出题逻辑。样例中未采集的多轮 Case 显示“待采集”，没有合成执行指标。

## 导入与兼容

- 同一个导入入口接受集合清单和运行轨迹，可先导入任一方；之后按 query_id 关联。
- 未加入集合的历史轨迹显示在虚拟“未分组”集合，轮次未标注；不从请求数或工具数推断。
- 同集合 ID、同清单内容重复导入幂等；同 ID 不同定义返回 409，新版本使用新的集合 ID。
- 不同集合引用相同 query_id 时，Case 定义须相同；冲突返回 409，整份清单回滚，不能部分写入。
- Case 可以没有运行记录。浏览集合、Case 或轨迹不会触发分析。
- 现有 trace 1.0 / 1.1 的 payload、hash、query_id、env_id、分析缓存都保持原样。

## 接口

完整请求、响应、错误与幂等约定见 [数据接口定义](api/README.md)，机器契约见 [OpenAPI](api/openapi.json)。

| 接口 | 返回 |
|---|---|
| `GET /api/collections` | 集合列表、Case 数与运行数 |
| `GET /api/collections/{id}` | 集合定义与 Case 列表、单轮 / 多轮信息 |
| `GET /api/cases/{query_id}` | Case 定义、所属集合、全部运行元数据 |
| `GET /api/catalog-schema` | 机器可校验的清单协议 |
| `GET /api/catalog-example` | 可下载的评测集清单示例 |
| `POST /api/import` | 根据 schema_version 校验并导入清单或轨迹 |

运行对比仍使用现有 `/api/compare`，后触发分析仍使用 `/api/runs/{id}/analysis`。
