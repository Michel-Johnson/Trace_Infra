# 执行尝试与结果提交

状态：后端 013 已实现，尚未上线。HTTP 1.10.0。官方 Worker 已在[014](official-worker.md)接入，远端任务凭据回写与超时回收见[018](remote-execution.md)。

每个 Invocation 固定操作版本、输入引用和配置；一次领取产生一个 attempt。
执行端提交产物内容，平台按固定操作的输出角色登记 Artifact，随后返回可查询的结果凭证。
`succeeded` 表示执行方成功报告被接收、要求的输出数量满足，并不表示任务质量合格。

## 服务调用

路径前缀：`/api/v1/projects/{project_id}/invocations/{invocation_id}`。
Bearer 身份绑定项目；凭据续发后仍以 principal 身份识别所属执行方。读取和提交权限分开。

| 方法与后缀 | 请求 | 权限 |
| --- | --- | --- |
| POST `/claim` | `worker_id`、`lease_seconds`（默认60，1–300秒）；`Idempotency-Key`请求头 | `invocations:execute` |
| POST `/heartbeat` | `attempt`、`lease_id` | `invocations:execute` |
| POST `/complete` | `attempt`、`lease_id`、`outputs` | `invocations:execute` |
| POST `/fail` | `attempt`、`lease_id`、`error: {code, details}` | `invocations:execute` |
| POST `/cancel` | `expected_attempt`（未领取为0） | `invocations:write` |
| POST `/retry` | `expected_attempt` | `invocations:write` |
| GET `/attempts` | `limit`默认50、最多100，`before`尝试编号 | `invocations:read` |
| GET `/results/{attempt}` | 无 | `invocations:read` |
| GET `/events` | `limit`默认50、最多100，`after`事件序号 | `invocations:read` |

领取和首次结果提交返回201，完全相同的重复提交返回200。不同结果或过期尝试返回409。
读分页是实时观察，不是冻结的历史快照。尝试倒序、事件正序；下一页参数由`next_before`或`next_after`给出。
具体请求/响应由 [OpenAPI](../../contracts/openapi.json) 中 ClaimRequest、CompleteRequest、ExecutionReceipt 等定义。

## 提交示例

```json
{
  "attempt": 1,
  "lease_id": "lease_example",
  "outputs": [
    {
      "role": "report",
      "content": {"media_type": "text/plain", "encoding": "base64", "data": "T0s="},
      "metadata": {"format": "short-report"}
    }
  ]
}
```

每个输出只接受角色、原始内容和metadata。平台从固定OperationVersion取`artifact_type`，从Invocation取全部有序inputs和config；producer_claim记录操作ID/版本，submitted_by记录真实服务身份。客户端不能替换这些字段。

执行方仅有`invocations:execute`也可提交其已领取任务的产物，不另需`artifacts:write`；这项权限不授予查询轨迹正文、读取产物或修改任务的权限。实际Worker需另外申请其所需的读取权限。

合计内容最多8 MiB、输出最多100项、请求信封最多16 MiB。每份metadata最多64 KiB，使用规范Base64；结果回执只包含产物引用，不携带正文。完整内容走Artifact读取接口。

## 并发、超时和恢复

- 同一请求键、执行方和领取参数返回同一attempt；不同参数返回409。重复领取不会延长租约，续租需调用heartbeat。
- `lease_id`是公开的防串写标识，不是访问凭据。每次回写仍检查项目权限、领取方principal、尝试编号、租约和当前任务状态。
- 使用数据库时间判断到期。读取到过期只显示`lease_state: expired`，不修改状态或自动重跑。heartbeat不能复活已到期租约。
- 重试是显式操作。失败、取消、blocked或租约过期任务可回到pending；旧attempt保留，新的领取递增编号。成功任务不能直接重试；重新计算需另建Invocation。
- 管理操作检查`expected_attempt`，防止对下一次执行误取消或误重试。未领取任务可用0取消。
- 已接收结果不可改写。同一结果重传返回原始回执，即使它属于已失败后重试的旧attempt；未接收的旧结果不能覆盖新attempt。
- 提交取得任务行锁时校验租约。在该事务内，即使写产物时到达租约期限，也允许这次已受理提交完成；竞争领取、取消或重试必须等待同一行锁。
- 产物描述、来源边、结果回执、状态和事件在一个事务中提交。中途失败全部回滚；内容库可能留下无引用的不可变字节，由后续清理机制处理，不能把它当成已发布产物。
- 心跳不追加事件；事件保留领取、取消、重试和最终提交。数据库时间是平台调度记录，不是模型输出时间或来源工具耗时，未据此补造token。

这套机制确保平台内唯一有效尝试和幂等结果接收，不能保证外部工具动作只执行一次。远端动作的重试策略、业务幂等和环境恢复仍由对应Worker或插件负责。
