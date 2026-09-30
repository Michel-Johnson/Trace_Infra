# 远程任务输入协议

状态：017输入协议及018执行权限扩展已实现，HTTP1.13.0，未部署。远程Agent使用独立任务凭据读取固定输入；不需要平台管理员凭据或整个项目的读取权限。显式执行权限、续租和结果回写见[远程执行](remote-execution.md)，跨系统派发继续019。

## 发放与生命周期

执行者先通过[执行协议](execution.md)领取Invocation，再调用：

`POST /api/v1/projects/{project_id}/invocations/{invocation_id}/task-grants`

```json
{
  "attempt": 1,
  "lease_id": "领取时返回的lease_id",
  "runtime": {
    "kind": "runtime_binding",
    "id": "analysis-service",
    "revision": 1,
    "digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "ttl_seconds": 3600
}
```

runtime摘要为占位值，使用登记返回的准确ref。发放者必须是当前attempt的领取者，并具有invocations:execute、operations:read、runtimes:read，以及输入类型对应的traces:read/artifacts:read。管理员身份不能替代另一个领取者来发放凭据。

平台检查当前租约、最新已启用RuntimeBinding，以及最近300秒内最后完成的成功能力观察；其中协议和准确OperationReference必须匹配。默认发放仅包含inputs:read；可显式设置allow_execution:true申请attempt:execute，此时远端还须声明trace-hunter/task-execution/1。发放不联网、不派发、不运行插件。观察说明服务曾声明该能力，不证明远端代码或任务质量。

返回201和`{token, grant}`。token形如`th_ta_…`，仅在发放响应中返回，数据库只保存其SHA-256。同一attempt再次发放是**轮换**，原凭据同时撤销；响应丢失时重新发放即可。此接口不使用Idempotency-Key，不保存或重放明文秘密。一个attempt只能绑定一个固定runtime，切换部署需显式取消/重试形成新attempt。

TTL为1–86400秒，默认3600秒；实际读取还受当前租约截止限制。续租不会延长凭据的绝对有效期。每次读取检查：凭据未撤销/过期、attempt仍为当前且running、租约未过期、Invocation仍running、发放者的service principal未撤销且仍具所需权限。取消、完成、过期或重试都阻止旧凭据继续读取。只读请求不续租、重试或登记消费。

显式撤销：`POST .../task-grants/{grant_id}/revoke`，由原领取者使用执行权限调用，可重复撤销。撤销整个service principal也会阻止其派生任务凭据；轮换或撤销某张长期service credential不自动撤销该principal已发出的独立任务凭据。

运行中的任务始终固定原runtime版本。登记新部署版本或新的探测结果影响后续凭据发放，不取消现有任务；停止任务使用cancel或revoke。鉴权在每次请求读取前执行，已经通过检查的在途读取可能完成，不承诺撤回已送出的字节。

## 远程Agent如何读取

所有下列接口只接受任务Bearer，不接受服务或管理员凭据。默认的只读任务凭据不能访问项目查询、其他Invocation、旧管理接口、任务创建、结果提交或训练消费。执行权限仅用于独立的任务执行接口，不扩大输入读取范围。平台通过输入序号定位资源，客户端不能提交任意run_id或latest。

| 接口 | 返回 |
| --- | --- |
| GET `/api/v1/task` | 当前任务摘要、固定操作定义、配置、输入数量、凭据及租约截止 |
| GET `/api/v1/task/inputs?limit=50&after=…` | 原输入顺序、role与固定ref，最多100项/页 |
| GET `/api/v1/task/inputs/{position}` | 输入的完整资源描述 |
| GET `.../{position}/members?limit=50&after=…` | 冻结选集成员及准确轨迹版本，最多100项/页 |
| GET `.../{position}/records?limit=50&after=…` | 轨迹原始spans数组，保留原序、字段和未知值 |
| GET `.../{position}/content?offset=0&limit=65536` | 原始字节分块，以Base64返回；最多256KiB/块 |

选集的描述、records和content读取支持`member_position`，表示冻结清单中的成员序号；省略时content读取的是选集清单本身。`after`和输入/成员position均为零起始序号。第一页省略after，后续使用next_after；null表示结束。

选集授权依据经原文digest核验的manifest，查询投影不作为权限权威。新导入、补采或投影损坏不会把其他轨迹版本加入授权。Artifact只授权该产物本身；其血缘中出现的轨迹或其他产物必须显式作为任务输入，才可以读取。

records返回`array_path: /spans`、record_count、source_array顺序和分页位置。页面最多100项，items规范JSON合计最多256KiB。单条记录大于预算时保留该position，返回`delivery: content_only, value: null`，用content分块恢复完整原件；普通记录为inline。不能把content_only当作空记录或遗漏记录。结构化读取目前需要解析不超过64MiB的JSON；更大的内容用分块接口。

content返回固定ref、原件digest/大小/media_type、offset、length、data_base64、next_offset。客户端按offset拼接解码字节，并核对完整digest。字节可能从UTF-8字符中间分块，先拼接再解析文本/JSON。到达文件末尾返回空块及next_offset:null；越过末尾报422。每次读取使用内容提供方的完整校验，当前文件实现会扫描原件核对摘要，分块限制的是响应大小，不代表仅进行一次小范围磁盘读取。

所有接口返回Cache-Control:no-store。正文丢失或摘要损坏会报错；平台不把缺失补成成功、零值或空轨迹。固定来源、任务读取与执行结果是不同事实，读取不会成为模型训练消费记录。
