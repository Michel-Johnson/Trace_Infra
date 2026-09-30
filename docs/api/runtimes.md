# 远程服务绑定与能力发现

状态：016实现，HTTP1.11.0，已随 0d0148e 核心发布上线。平台保存连接配置、探测事实和远程声明；查询现有结果不联网、不派发任务。按任务限定的输入凭据与分页读取见[任务输入协议](task-access.md)，执行回写见[远程执行](remote-execution.md)，网络派发继续后续轮次。

当前上线合同为 1.15.0，部署范围见[发布说明](../project/release-20260914.md)；上文轮次与早期合同号保留为实现沿革。

## 绑定和版本

OperationVersion固定操作合同与实现标识；RuntimeBinding记录部署地址、认证引用和开关。地址、凭据引用、开关或metadata改变时，追加绑定revision，旧配置与探测记录仍可查询。凭据内容轮换可以保持同一secret_ref，不修改操作定义。

管理员POST `/api/v1/projects/{project_id}/runtimes/{runtime_id}/revisions`，带Idempotency-Key：

```json
{
  "expected_previous": 0,
  "config": {
    "name": "分析服务",
    "endpoint": "https://runtime.example/service",
    "enabled": true,
    "auth": {"kind": "bearer", "secret_ref": "analysis-service"},
    "metadata": {"region": "example"}
  }
}
```

首次创建的expected_previous为0；后续填当前revision，并使用新请求键。比较版本和幂等登记在同一事务完成；同键同请求返回原绑定，不随最新配置变化。绑定引用是`kind: runtime_binding + id + revision + digest`，与轨迹/产物输入引用分开。

地址仅接受HTTP(S)基础路径，不包含用户密码、查询参数或fragment。认证为none或bearer的secret_ref。真实token由服务端环境变量TRACE_HUNTER_RUNTIME_SECRETS_FILE指定的私有JSON文件读取；只在发往已登记地址的请求头使用，不进入绑定、探测记录、URL或错误详情。此文件是平台部署配置，客户端不能提交文件路径。

## 远程服务返回什么

显式POST `.../{runtime_id}/revisions/{revision}/probe`后，平台GET：

`<endpoint>/.well-known/trace-hunter-runtime.json`

远程返回application/json：

```json
{
  "schema_version": "trace-hunter/runtime-capabilities/1",
  "name": "Analysis runtime",
  "version": "1.0.0",
  "protocols": ["trace-hunter/remote-runtime/1"],
  "operations": [
    {
      "operation_id": "example.analysis",
      "version": "1.0.0",
      "digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  ]
}
```

示例摘要为占位值，需使用平台已登记操作的真实ref。操作声明最多100项，协议最多8项，整体规范JSON最多64KiB，实际HTTP响应最多128KiB。操作身份与版本不能重复。远程部署可承载已安装worker类操作，匹配准确操作定义即可，不因部署位置改变而重写原操作。

探测是一次GET，不传轨迹、模型请求、输入正文或计算任务。总截止10秒，包含慢速分块响应；不跟随重定向、不接受压缩响应、不使用客户端机器的隐式代理环境变量。失败保存稳定错误码和可用的HTTP状态，不保存远程错误正文。API返回200表示探测记录已收录，需看记录state判断探测是否成功。

## 查询与筛选

| 接口 | 权限与用途 |
| --- | --- |
| POST `.../{runtime_id}/revisions` | 管理员；追加连接版本 |
| GET `/api/v1/projects/{project_id}/runtimes` | runtimes:read；最新绑定摘要及最后完成的观测 |
| GET `.../{runtime_id}/revisions` | runtimes:read；绑定历史，倒序 |
| GET `.../{runtime_id}/revisions/{revision}` | runtimes:read；固定配置 |
| POST `.../{runtime_id}/revisions/{revision}/probe` | runtimes:probe；发起新的远程能力读取 |
| GET `.../{runtime_id}/revisions/{revision}/probes` | runtimes:read；探测摘要，倒序 |
| GET `.../{runtime_id}/revisions/{revision}/probes/{sequence}` | runtimes:read；探测详情和完整有效声明 |
| POST `/api/v1/projects/{project_id}/runtimes/discover` | runtimes:read + operations:read；按准确操作ref查询匹配候选，不联网 |

发现请求为`{operation: <OperationReference>, max_age_seconds: 300, limit: 50, cursor: null}`。返回已启用的最新绑定，且其最后完成的探测成功、协议匹配、声明包含该准确操作、记录未超过指定时效。默认300秒，可选1–3600秒。响应是live_keyset，最多100条；游标绑定项目、操作和时效条件。历史页用next_before，绑定列表/发现页用next_cursor。

绑定列表用于查看未探测、失败、过期或禁用的服务；发现接口只给出满足当前查询条件的候选。启用一个新绑定版本后，必须对该版本取得观测，旧版本的探测不会被挪用。新失败结果不会退回选择较早成功结果；并发探测按发起序号确定新旧，晚到的旧响应不能覆盖新观测。

## 结果的边界

verification始终为advertised：远程声明与固定操作合同匹配，不代表平台已核验其代码安装、模型额度或真实任务质量。探测成功表示那次读取完成，fresh表示观测未过期，不保证服务此刻可执行。未探测或探测失败时，protocol_supported和operation_count保留null，不把未知当作不支持或零操作。

发起后尚未收录结果的探测标incomplete，不把数据库记录当成进程仍在运行的证据。正常网络失败会记录failed；平台中断或结果事务失败留下incomplete。再次probe是新的观察，读取不改写旧记录，也不自动补探测。普通查询可继续使用时效内的最后完成观测，实际派发仍需后续任务凭据和执行确认。
