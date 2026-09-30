# 通用产物与固定来源

状态：第 011 轮已通过隔离验收，尚未部署。分类、评分、报告、渲染说明及其它插件输出共用 Artifact；平台不要求它一定包含分数。

产物内容是不可变字节，保存在 ContentStore。描述绑定固定输入引用、声明的生成者、配置和元数据，修正或重算产生新产物，不覆盖旧结果。读取产物或查询已有报告不会调用模型、插件，也不登记训练消费。

## 引用及身份

统一资源引用为 `{kind,id,revision,digest}`，项目由请求路径确定，所有上游必须属于该项目。

| kind | revision | digest 的含义 |
|---|---|---|
| trace_revision | 准确的正整数平台版本 | 该版本原件的 content.digest |
| selection_snapshot | null | 固定成员清单的 manifest.digest |
| artifact | null | 绑定内容、来源和生成配置的 descriptor_digest |

不可变选集和产物没有可变版本指针，null 不代表缺少采集证据。上游必须已登记且摘要完全一致，不能用 latest、任意外部地址或仅声称存在的摘要替代固定引用。声明新产物时，平台在同一事务验证实体、版本、项目和摘要。

`content.digest` 只标识正文。`descriptor_digest` 标识稳定计算描述：`schema_version: "trace-hunter/artifact-spec/1"`、project_id、artifact_type、content、有序 inputs、producer_claim、config 和 metadata；不包含随机 artifact_id、created_at 或提交者。同样的正文如果对应不同输入或配置，其描述摘要不同。同一描述也可以有多个独立提交实例。

这一版沿用仓库的 canonical JSON 口径：UTF-8、对象键按 Unicode 顺序排序、无额外空格、非 ASCII 不转义、禁止非有限数值；数字保留现有 Python JSON 表示（例如1和1.0不合并），不是RFC8785。输入数组顺序参与摘要。跨语言实现必须通过同一组字节/摘要样例；客户端引用时使用服务返回的摘要，不自行换一种规范化算法。

`inputs` 是带 role 的有序引用，最多100项；空列表允许独立产物，不推断其外部来源可靠性。`producer_claim` 记录名称与版本，未知保留 null；`submitted_by` 由服务端身份产生，和声明的生成者分开。011 的 provenance 为 declared、schema_validation 为 envelope_only，只证明信封和内部引用符合平台约束，不表示评分正确、渲染安全或远程执行已核验。实际 Invocation / attempt 绑定在后续轮次接入。

## 写入与读取

POST `/api/v1/projects/{project_id}/artifacts` 使用单次 JSON 信封：

```json
{
  "artifact_type": "example.report/1",
  "content": {
    "media_type": "text/plain; charset=utf-8",
    "encoding": "base64",
    "data": "b2sK"
  },
  "inputs": [],
  "producer_claim": {"name": "example-analyzer", "version": "1.0"},
  "config": {},
  "metadata": {}
}
```

项目须已登记。必填 Idempotency-Key；首次201，同键同描述200，同键改变正文、输入或配置409。信封按实际接收流限制16MiB，解码正文最多8MiB；config与metadata各最多64KiB规范JSON。Base64仅作为传输包装，正文空白、换行和二进制按原字节保留。暂不接受任意已知CAS摘要充当上传，避免把知道摘要误当作访问授权。

写入要求 artifacts:write；若有轨迹/选集上游还需 traces:read，有产物上游还需 artifacts:read。读取仅按项目中的已登记产物定位内容，不提供全局按digest下载入口。

- GET `.../artifacts/{artifact_id}`：固定描述、输入、配置与来源声明。
- GET `.../artifacts/{artifact_id}/content`：校验摘要和大小后的原始正文，按附件返回。
- POST `.../artifacts/query`：只读查询已有产物摘要，需 artifacts:read。

查询不返回正文、大配置或完整输入列表。支持类型、生成者/版本、配置摘要、准确上游引用过滤，默认50项，最多100项；按 created_at、artifact_id 稳定分页，cursor绑定项目和过滤条件。它是实时查询，不是冻结发布清单。

数据库事务原子发布产物、来源边和幂等记录。内容写入完成但事务失败时可能存在未引用对象；它不是已发布产物，后续内容回收处理。原件读取不可用或损坏明确失败，不把已登记产物降成不存在。
