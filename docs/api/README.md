# Trace Hunter Core API

当前合同版本为 **4.0.0**。评测入口收敛为选样、对象取证、前后步骤、正文搜索、指标统计和批量证据导出。

- [评测接口与配套接口取舍](evaluation-surface.md)
- [全部剩余接口及作用](remaining-endpoints.md)
- [机器合同](../../contracts/openapi.json)
- [对象查询、指标与证据导出](advanced-query.md)
- [CLI](cli.md)
- [统一任务进度](tasks.md)

Skill 版本、导入与自动导入、原件回读、项目、网页 Agent、任务管理及健康检查保持。导入、浏览、搜索不隐式触发评测。已删除的专用分析接口不再注册，也不在能力声明、CLI、前端和 Skill 中作为可用入口出现。历史数据与产物保留。
