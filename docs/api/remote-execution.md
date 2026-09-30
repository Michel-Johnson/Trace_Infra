# 远程执行、结果恢复与超时回收

状态：018实现，HTTP1.13.0，已随 0d0148e 核心发布上线。远程执行复用Invocation、attempt和结果事务；平台不维护第二套远程任务状态机。任务发放和输入协议见[任务输入](task-access.md)，服务身份接口见[执行协议](execution.md)。

当前上线合同为 1.15.0，部署范围见[发布说明](../project/release-20260914.md)；上文轮次与早期合同号保留为实现沿革。

## 显式授权

默认任务凭据仍只有inputs:read，旧凭据不自动升级。领取者在发放请求中设置`allow_execution: true`，才会获得`attempt:execute`。这一权限单独持久化；再次发放时省略或设置false，会轮换为只读凭据并使旧凭据失效。

远程能力声明必须同时包含`trace-hunter/remote-runtime/1`和`trace-hunter/task-execution/1`，并列出准确OperationReference。平台在发放时检查已启用的最新runtime版本及300秒内最后完成的成功观察。声明只读协议的服务可以获得输入凭据，不能获得执行权限。

执行凭据仍受绝对TTL、撤销和发放者principal权限限制。普通输入读取要求当前attempt和租约有效；执行专用的状态/结果查询允许查看该凭据原attempt的终态，便于停止工作和核对回执。这不授予其他任务、其他attempt正文或原输入的额外访问权。

## 远端调用

使用任务Bearer，前缀`/api/v1/task`：

| 方法与后缀 | 请求 | 返回 |
| --- | --- | --- |
| GET `/state` | 无 | 原attempt、当前attempt编号、Invocation状态、can_execute及should_stop |
| POST `/heartbeat` | `{}` | 续租后的attempt描述 |
| POST `/complete` | `outputs` | 固定结果回执及远程委派来源 |
| POST `/fail` | `error: {code, details}` | 固定失败回执及远程委派来源 |
| GET `/result` | 无 | 原attempt的已接收回执及委派来源；未接收时两者为null |

客户端不传project、invocation_id、attempt或lease_id来选择执行目标，平台直接从凭据绑定解析。请求信封不接受这些额外字段。服务Bearer、浏览器管理员身份和仅有inputs:read的凭据都不能使用执行通道。

成功输出示例：

```json
{
  "outputs": [{
    "role": "report",
    "content": {"media_type": "text/plain", "encoding": "base64", "data": "T0s="},
    "metadata": {}
  }]
}
```

输出角色和数量受固定操作合同约束，类型、输入血缘、配置与生产者由平台绑定。合计内容最多8MiB、输出最多100项、信封最多16MiB，复用服务执行接口的解析和事务边界。成功报告被接收不表示质量合格。

can_execute是一次状态观察。实际续租或回写会再次取得任务锁并校验租约；状态查询返回后发生取消/过期时，回写仍会被拒绝。远端应在租约截止前主动续租，并在should_stop为true或续租失败后停止处理。停止接受回写不能证明远端进程或外部动作已经停止。

## 原子结果与委派来源

首次结果返回201；同样的结果重传返回200及原回执，改写结果返回409。结果、Artifact、血缘、生命周期变化以及远程来源证据在同一数据库事务提交。来源写入失败时，前面的结果与产物登记也回滚。校验和提交期间持有同一任务锁，凭据轮换或撤销不能插入校验与另一笔提交之间。

ExecutionReceipt保留原reported_by，表示授权领取者。新的delegation明确记录grant_id、固定RuntimeReference、探测序号和receipt_digest，并有自己的内容摘要。它证明平台通过这份委派接受报告，不证明远端安装代码、运行环境或报告质量已经核验。

若结果此前由原领取者直接提交，远端重传相同结果只返回原回执，delegation保持null；不能给已有本地结果补造远程来源。凭据轮换不改写第一次已接受的来源证据。

项目身份具备invocations:read时可查询：

`GET /api/v1/projects/{project_id}/invocations/{invocation_id}/results/{attempt}/remote`

因此任务凭据过期后，授权管理端仍可核对结果与来源。

## 断线、取消与过期

提交响应丢失时，远端可以用仍有效的执行凭据查询`/result`，或重传完全相同的结果。已接受的失败报告在新attempt开始后仍可核对；原attempt未被接受的迟到结果不能写进新attempt。绝对凭据过期或被撤销后，任务通道返回401，由项目管理端核对。

取消沿用服务身份的`POST .../invocations/{id}/cancel`及expected_attempt。取消不生成虚假的失败报告；原执行凭据可查看should_stop，但续租和新结果提交返回409，输入读取被拒绝。执行结果已经成功接受后不能取消，重新计算需要新Invocation。

显式回收租约过期任务：

`POST /api/v1/projects/{project_id}/invocations/recover-expired`，请求`{"limit":100}`，需要invocations:write。

最多扫描100个候选，逐任务重新加锁核对候选attempt及截止时间。仍然过期时把attempt标expired、Invocation标blocked，并写lease_expired事件；不生成模型失败结论，不自动重试，也不认定外部动作没有发生。之后需管理端显式retry，下一次领取才产生新attempt。

返回items、examined、limit和more_candidates。并发扫描可能重叠，更多过期任务也可能随后出现，需显式再次调用；more_candidates描述当次扫描，不保证整个队列已清空。每个任务单独事务提交，一批中途故障时已完成的回收保留，再次调用不会重复回收同一attempt。

跨系统派发、动作去重和不明结果对账继续019；本接口的幂等结果回写不能保证外部动作只执行一次。
