# Trace Revision

`trace_revisions` 是原始 Trace 的不可变版本记录。原始字节保存在内容存储，表内保存 digest、大小、格式、来源类型、幂等键和元数据；`traces.latest_revision` 只负责定位最新版本。

## 写入

`POST /api/v1/projects/{project_id}/traces` 必须提供 `Idempotency-Key`。同键、同内容重试返回已有 revision；同键不同内容返回 409。追加版本必须提交准确的 `expected_previous`，并发竞争只能有一个成功。

写入成功后同步生成当前 `trace-index/4`。投影失败不改变已接收的原件，调用方可通过 `POST .../index` 显式重建。

## 读取

- `GET .../revisions`：分页读取历史版本。
- `GET .../revisions/{revision}`：读取固定版本和投影状态。
- `GET .../content`：校验 digest 后返回原始字节。
- `POST .../index`：重建对象、关系与搜索投影。

读取不会跟随外部 locator、补采数据或触发分析。`trace_objects`、`trace_edges`、`trace_search_documents` 损坏时应从 revision 原件重建，不能修改原始内容。
