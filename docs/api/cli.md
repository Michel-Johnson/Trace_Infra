# Trace Hunter CLI

当前评测命令为 trace query、object-query、span-window、search、metrics-query、evidence-export。版本检查、项目、导入、普通浏览、搜索索引和任务管理命令保留。

完整参数见 [CLI Skill 参考](../../skills/trace-hunter-cli/references/cli.md)，接口取舍见[评测入口](evaluation-surface.md)。运行 capabilities 发现当前字段；使用 --all-pages 完成分页，保留原始退出码与错误信息。

评测任务用 task create/update/watch 上报进度，语义评分由评测器执行。批量证据导出成功后再下载 content，不能将创建任务成功当成已获得证据。

原始文件统一用 upload；--source-format 指定已有 Adapter，不启动 Agent，auto 才启动 Agent。旧 adapter-import 命令只保留兼容包装，内部使用同一上传协议，并保留旧默认幂等键。状态、取消、重试统一用 task get/cancel/retry。标准 v2 文件仍使用 import 入库，详见[导入进度与迁移](import-progress.md)。
