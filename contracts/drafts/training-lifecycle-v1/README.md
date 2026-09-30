# 训练生命周期协议草案

版本：`trace-hunter/training-lifecycle/0.1-draft`。对应[训练轨迹基础设施](../../../docs/architecture/training-data-platform.md)。**只用于离线设计与语义验证，当前 HTTP / MCP 不接受这些新对象，没有执行回放或训练。**

[lifecycle.schema.json](lifecycle.schema.json) 是可组合的六类对象；每次校验一个对象，不要求每次查询返回整个流程：

| kind | 内容 |
|---|---|
| selection | 查询后锁定的轨迹版本与选择清单 |
| analysis_request | 固定选择范围、分析器/配置版本及显式计算请求 |
| replay | 回放条件、模式、尝试、状态及输出的新轨迹引用 |
| validation | 对指定回放版本的验收策略、结论和检查证据 |
| dataset | 固定样本清单、转换/profile、发布策略与准入报告 |
| consumption | 送达、训练确认、纠正事件及训练端证据 |

原始轨迹仍使用既有 Trace 协议；已有分类/评分输出仍用对应产物协议。本草案不重复定义消息/token Schema，也未定义完整 Query DSL、AnalysisArtifact 索引、权限、任务调度、分片领取或真实训练格式。`selection.trace_refs` 是小规模示意；正式大范围选择以 manifest 和分页成员索引实现。

空查询快照合法；后续计算或发布能否接受空输入，由各自要求决定，不给空集合伪造训练样本。

`id` 是平台资源身份；`receipt_id` 是训练端稳定逻辑回执身份。幂等范围为 `(consumer_run_id, consumer_attempt, receipt_id)`，与跨资源引用分开。事件不可修订，revision 固定 1；纠错使用新事件引用原回执。送达数量和训练确认数量分列，跨 epoch 的真实重复使用按次数保留，不冒充去重样本数。

## 合成示例与校验

[workflow.example.json](workflow.example.json) 的流程是：冻结选择 → 申请分析 → 回放结果 → 验收 → 数据发布 → 送达 → 训练确认。所有资源和结论均为合成值；`external_resources` 只是带摘要的占位内容，并非真正的环境镜像、Trace、训练样本或验收证据。

```bash
.venv/bin/python scripts/build_training_lifecycle_draft.py
.venv/bin/python scripts/validate_training_lifecycle_draft.py
.venv/bin/python -m unittest discover -s tests -p 'test_training_lifecycle_draft.py' -v
```

预览得到 2 个送达样本使用次数、2 个训练端确认使用次数。重复提交相同回执不会变为 4；去掉训练确认回执后，训练使用次数为 0。这里的计数是合成账本语义，不是测得的训练行为。

离线验证覆盖：对象形状、日期/引用/摘要、分析范围绑定、回放环境条件、新输出身份、验收不能把 unknown 当 passed、账本重传/冲突、跨任务/epoch 重用、带原引用的纠正。合成资源摘要采用与生成器一致的 UTF-8、排序键、紧凑 JSON；不重新定义已有 Trace 的摘要规范，也不宣称这是已冻结的跨语言 canonicalization 标准。

不覆盖：内容真实性、真实环境可恢复性、工具副作用、验收器执行、样本成员及 token 合法性、训练实际发生、数据库并发/租约、断点对账或网络故障。校验器只接受显式 synthetic 的输入，输出 `live_execution:false` 与 `source_attestation:not_performed`，不能据此给真实轨迹标记训练可用或已消费。

冻结服务协议前，以真实环境和训练端执行主设计中的纵向验收；HTTP 路径和 MCP 工具由届时的共享契约生成，当前线上 `contracts/openapi.json` 保持不变。
