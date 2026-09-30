# 轨迹查询 API

状态：第 007 轮已实现并通过隔离环境验收，已随 0d0148e 核心发布上线。查询是读取投影，不启动分析或重建索引。

当前上线合同为 1.15.0，部署范围见[发布说明](../project/release-20260914.md)；上文轮次与早期合同号保留为实现沿革。

- GET `/api/v1/query-capabilities`：查询字段、过滤器、限制与排序规则。
- POST `/api/v1/projects/{project_id}/traces/query`：需要该项目的 `traces:read` 权限。POST 只用于承载查询条件，无写入副作用。

```json
{
  "filters": {"index_state": ["complete"], "harness": ["Doubao Work"]},
  "fields": ["run_id", "revision", "content_digest", "index_state", "record_count"],
  "revisions": "latest",
  "limit": 50
}
```

不同过滤字段之间为 AND，同字段数组内为任一精确匹配。字段只接受能力接口列出的名称，不接受 SQL 或任意表达式。每页 1–100 行，每字段最多 50 个匹配值；返回字段按请求裁剪，不含正文。能力字段 max_item_bytes 表示选中的行数据累计最多 2 MiB，较大的页提前返回续页游标；单行选中字段超过限制则报错，请求者须减少字段。响应封装与游标不计入这 2 MiB 行数据预算。

响应包含 `items`、`next_cursor`、`query_digest`、`projector_version` 和 `consistency: live_keyset`。下页沿用过滤条件、字段与 revision 模式，携带 `cursor`；可以更换页大小。游标绑定项目和查询，不代表权限。

默认排序为 run ID 字节序、revision 升序。需要先看新采集的 Trace 时传 `"order":"created_at_desc"`，按登记时间倒序、同一时间按 run ID 字节序和 revision 升序稳定分页；CLI 为 `trace query --order created_at_desc --limit 20`。游标绑定排序方式，不能换排序后继续翻页。latest 模式每个 run 最多出现一次；默认排序下，已翻过的 run 后续补版本不会再次出现，尚未翻到的 run 可以显示新版。all 模式逐版本翻页。因此这不是固定数据集快照；回放、验收与训练须使用冻结 SelectionSnapshot。

`index_state` 为 `unindexed` 的轨迹仍可发现，计数为 null。零条记录的已完成索引返回 0。query/env/harness/model/status 直接读取登记版本时保存的元数据，尚未建立索引或索引失败也能正确过滤。记录计数依赖指定 projector；后续聚合接口提供索引覆盖与缺口统计。正文仍通过原件/版本接口按准确版本读取。

## 元数据和缺口（010）

query_id / env_id / harness / model / status 的匹配值接受最多 4,096 字符的字符串或数组内 null。`{"filters":{"model":[null]}}` 只匹配未记录主模型的版本，和字符串 `"null"`、`"unknown"` 不同；同样可用于聚合与冻结选集。空字符串保持精确匹配。空数组与 `{"filters":{"model":null}}` 仍为无效输入。

登记元数据写入独立查找表，不需要在查询中把整份 metadata 转成数据库 JSON；合法 NUL 和字面 Unicode 转义保持区别。前缀索引仅缩小候选范围，结果始终核对完整值，长文本不截断。GET/查询不补写该表。显式数据库迁移从已登记版本回填缺行，原件/摘要不变。

查找表缺行时，同一 SQL 快照中的完整性检查返回 503；不能因为过滤条件将缺行排除就报告零条。隔离范围为当前项目、latest/all 模式和非元数据过滤条件。重新运行显式迁移进行回填后可恢复查询；其他项目不受影响。损坏值与缺行分开报告，前者仍为 500。
