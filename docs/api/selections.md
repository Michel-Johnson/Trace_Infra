# 冻结选集

第 009 轮已实现并通过隔离环境验收，已随 0d0148e 核心发布上线。SelectionSnapshot 为后续分析、回放、验收和训练发布固定输入版本。它只冻结引用，不推断源正文当前可读、采集完整或训练合格。

当前上线合同为 1.15.0，部署范围见[发布说明](../project/release-20260914.md)；上文轮次与早期合同号保留为实现沿革。

POST `/api/v1/projects/{project_id}/selections`，权限 selections:write，必须提供 Idempotency-Key。输入只有 filters 和 revisions（latest / all），不接受分页游标或已截断的页面。

```json
{"filters":{"harness":["Doubao Work"]},"revisions":"latest"}
```

一次 SQL 读取决定全部成员。最多 10,000 个，超过则整体失败，不截断。空选择合法，member_count 为 0。首次创建返回 201，同 key 与相同查询返回 200 和原选集，即使后续已经导入新版；同 key 改查询返回 409。新增轨迹、补版本或重建索引都不会修改已冻结的成员。需要重新观察时使用新的 key 创建另一份选集。

响应给出 selection_id、query_digest、member_count、manifest（原件摘要与字节数）、实际服务身份或 operator / unknown。正文来源与冻结操作者分开记录，不伪造用户、模型身份。

需要 traces:read 的读取接口：

- GET `/api/v1/projects/{project_id}/selections/{selection_id}`：描述。
- GET `.../{selection_id}/members?limit=100&after=99`：按从 0 开始的 position 固定分页，返回 next_after 和 manifest_digest。
- GET `.../{selection_id}/manifest`：完整、经 SHA-256 和字节数校验的清单原件。

manifest 是固定成员的权威原件；members 是数据库分页投影。页读取校验位置和数量，不能把它描述成对照 manifest 的逐项密码学验证。消费端应获取并核验完整 manifest，再按其中 project/run/revision/content_digest 读取原件。后续派生产物、回放与发布记录引用该 manifest 摘要。

冻结时数据库事务原子登记选集、成员和请求幂等记录；写入失败不发布半份选集。内容存储与数据库不是一个事务，极少数失败可留下未引用的清单对象，交由后续回收流程处理，不能当作已发布选集。GET 不重建投影，不执行插件或来源记录中的工具。

元数据过滤支持数组内 null 和完整 Unicode 字符串，具体见 [查询规则](trace-query.md#元数据和缺口010)。010 起，冻结的成员读取与查找表完整性检查使用同一 SQL 快照；缺行返回 503，事务不留下选集、成员或幂等占位。已冻结选集读取不依赖当前查找表，不因重建或缺行改变。
