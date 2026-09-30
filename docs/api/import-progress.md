# Adapter 导入进度

本次合并四个 HTTP 操作：POST imports、GET imports/{job_id}、POST imports/uploads/{upload_id}/retry 和 /cancel。旧路径返回404；直接调用旧 HTTP 的客户端须迁移，不能仅更新请求地址而保留旧请求体。全平台由82减为78个操作，导入相关由15减为11个，评测接口不变。

Core API 4.0.0 起，原始文件统一使用 `imports/uploads`：创建上传、PUT 分块、POST complete。
CLI 使用 `upload FILE --source-format FORMAT --wait`，自动完成分块与 SHA-256 校验。
明确指定已注册格式只运行 Adapter，不启动 Agent；`auto` 才触发 Agent。绑定写在上传请求的 binding 中。
完成上传响应的 job_id 就是任务 ID；状态统一读取 `GET .../tasks/{task_id}` 或 tasks events。
重试和取消统一使用 `POST .../tasks/{task_id}/retry` 与 `/cancel`。指定 Adapter 失败不支持任务自动重试，需先纠正来源或绑定后重新提交。
前端可先读取 `GET /api/v1/adapter-import-capabilities` 获取支持的 Adapter、固定阶段和大小限制。

任务按真实执行状态报告六步：读取原文、Adapter 转 v2、Schema/来源校验、不可变存储、
对象投影、可搜索文本投影。统一任务快照持久化在内容目录旁，不新增业务表；原始字节和
转换后的 v2 Trace 分别写入内容寻址存储，Trace revision、对象和搜索文档仍写入核心表。

新任务的 result 保留 run_id、revision、content_digest、index_state，并通过 import_result 包含完整转换结果、Adapter 报告和原文引用。历史任务没有完整报告时不补造。
失败结果只返回稳定错误码、
安全说明与 Adapter diagnostics，不返回原始正文或数据库内部错误。

# 存储与索引健康

`GET /api/v1/projects/{project_id}/observability` 返回最新 revision 的 Trace/对象/搜索文档/
关系数量、投影状态、采集覆盖率，以及 PostgreSQL、`pg_trgm` 与 trigram GIN 索引的实际状态。
该接口只读，不触发导入、重建索引或分析。
