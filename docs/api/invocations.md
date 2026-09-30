# 操作版本与计算请求

状态：第012轮接通请求，第013轮接通[领取、租约与结果提交](execution.md)，均已通过隔离验收，已随 0d0148e 核心发布上线。登记和读取仍不运行实现；官方Worker已在[014](official-worker.md)接入。

当前上线合同为 1.15.0，部署范围见[发布说明](../project/release-20260914.md)；上文轮次与早期合同号保留为实现沿革。

OperationVersion 描述可调用能力，Invocation 描述针对固定输入的一次请求。操作可以生成分类、报告、评分、渲染说明或其它产物，不绑定评分任务。浏览器即时插件仍由Web宿主处理，不进入后台计算队列。

## 固定操作定义

管理员 POST `/api/v1/projects/{project_id}/operations` 登记定义。项目须已存在，operation_id/version 为最多128字符的ASCII URL标识；相同版本相同内容返回原定义，改变内容返回409，需要发布新版本。

```json
{
  "schema_version": "trace-hunter/operation/1",
  "operation_id": "example.record-counts",
  "version": "1.0.0",
  "implementation": {
    "host": "worker",
    "key": "example.record-counts",
    "package_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "input_roles": [
    {"role": "source", "kinds": ["trace_revision", "selection_snapshot"], "min_items": 1, "max_items": 1}
  ],
  "config_schema": {"type": "object", "additionalProperties": false},
  "outputs": [
    {"role": "report", "artifact_type": "example.record-counts/1", "min_items": 1, "max_items": 1}
  ]
}
```

此处摘要是说明结构的占位值，不是已验证的可执行包。host 是 worker / remote；key 是固定实现标识，不是可执行命令或远程地址。登记只校验定义，package_verification 为 declared。安装验证、调度与远程地址绑定独立处理。

定义最多64KiB规范JSON。输入角色声明允许的资源种类和数量，角色唯一，总输入最多100项。输出声明角色、产物类型与数量，为后续结果提交建立约束；本轮没有伪造结果。config_schema 按JSON Schema 2020-12规则校验；声明$schema时只接受https://json-schema.org/draft/2020-12/schema（可带末尾#）。实际子Schema及本地引用目标一起校验，不下载远程Schema；普通examples/default/const内容和属性名中的$ref、$schema按应用数据保留。配置须满足注册规则，不隐式填充默认值。

## 创建和读取请求

POST `/api/v1/projects/{project_id}/invocations` 必填 Idempotency-Key：

```json
{
  "operation": {
    "operation_id": "example.record-counts",
    "version": "1.0.0",
    "digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "inputs": [
    {"role": "source", "ref": {"kind": "trace_revision", "id": "existing-run", "revision": 1, "digest": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}}
  ],
  "config": {}
}
```

此处ID与摘要仅为合法结构示例，需替换为实际操作及资源响应中的值。

输入引用采用[通用产物](artifacts.md)的同一规则，支持轨迹版本、冻结选集和既有产物。每项在同一事务检查实际实体、项目、版本与摘要，同时验证角色、数量及配置。旧版本不会因补采或操作升级而改变。请求spec摘要包含项目、准确操作引用、有序输入及config；同键同请求返回首次创建的Invocation，改变任意绑定返回409。

首次201，重试200。新请求状态为pending；created_at是服务器登记时间，不当作模型开始时间。requested_by来自服务端身份，客户端不能自行指定。创建请求会保存Invocation及来源边，不生成Artifact或执行旧插件任务。

| 接口 | 权限 / 返回 |
|---|---|
| POST `.../operations` | 管理员；登记操作版本 |
| GET `.../operations` | operations:read；能力摘要，不展开配置Schema |
| GET `.../operations/{operation_id}/versions/{version}` | operations:read；完整定义和固定摘要 |
| POST `.../invocations` | invocations:write + operations:read；另须每种上游的读取权限 |
| GET `.../invocations` | invocations:read；请求摘要 |
| GET `.../invocations/{invocation_id}` | invocations:read；固定输入、配置及状态 |

上游轨迹/选集要求traces:read，产物要求artifacts:read，所有权限限定同项目。两个列表默认50项、最多100项，cursor绑定项目和列表种类；列表是live_keyset。读取不创建请求、分析或消费记录。通用Artifact和Invocation复用固定引用解析，不维护第二套版本/摘要语义。

旧Manifest v2仍为现有插件服务合同，012没有偷偷转换旧执行记录或将其当作新Invocation。后续通过明确适配，把已安装贡献点绑定为操作版本，再由官方或远程执行器领取任务。
