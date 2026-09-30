# 对象查询、指标与批量证据

项目内前缀为 /api/v1/projects/{project_id}。

- POST objects/query：按时间、属性、固定字段与快照查询对象；payload 可读取已采集内容。
- POST metrics/query：计数、错误率、耗时分位数、直方图和已采集用量；同时报告覆盖情况。
- POST evidence-exports：生成固定选集的对象与正文证据包。
- GET evidence-exports/{task_id}/content：下载完成的证据文件。

同一查询后续分页复用响应 snapshot 和 next_cursor，不能改变选集后复用旧游标。用显式 revision 过滤绑定单条证据；批量样本清单逐条固定版本。未知状态不计入成功/失败分母，字段缺失不当作 0。

导出返回任务，可通过 tasks 查询进度、取消或重试。语义判断、验收通过率和行为分类由评测器基于这些事实完成；现有统计接口不自动评分。跨分组汇总时不能平均分位数。

参见[评测入口](evaluation-surface.md)及[全部接口](remaining-endpoints.md)。
