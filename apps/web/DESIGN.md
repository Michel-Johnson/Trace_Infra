# Trace Hunter 工作空间

## 参考与取舍

- [Linear Projects](https://linear.app/docs/projects) 和 [Custom Views](https://linear.app/docs/custom-views)：阅读官方页面及产品截图。采用稳定的功能导航、列表内筛选、进入项目后查看成员条目、局部详情。保留浅色数据工作区。
- [Langfuse Observability](https://langfuse.com/docs/observability/overview)：查看官方轨迹界面截图。采用轨迹列表到具体记录的入口，详情中保留调用序列及时间、输入和输出。
- [Vercel Projects](https://vercel.com/docs/projects)：工作空间列出项目，选择项目后才展开项目内的运行与配置。采纳对象层级，不复制营销网站卡片。
- [getdesign.md 的 Linear 预览](https://getdesign.md/design-md/linear.app/preview) 主要分析品牌和营销页面，只作为颜色、边界和强调色参考，不能代替产品信息架构。

## 信息架构

侧栏只放五个常驻功能：工作台、轨迹分析、轨迹库、插件、采集接入。集合、Case、模型、运行记录均不放入全局侧栏。文档与协议收在底部的帮助入口。

| 页面 | 主要内容 | 下一步 |
|---|---|---|
| `/` 工作台 | 真实数量、最近导入 | 进入任务列表或最近一条轨迹 |
| `/collections` 轨迹分析 | 分析集 / 任务集大列表，类型筛选、搜索 | 点击集合 |
| `/collections/:id` | 集合内的 Case 列表，单轮 / 多轮、运行数、采集状态 | 点击 Case |
| `/collections/:id/cases/:query_id` | 用户输入、模型 / Harness 运行对比、调用方格 | 查看调用详情，显式触发分析 |
| `/traces` 轨迹库 | 所有运行，Harness 筛选、搜索、导入时间 | 定位一条运行，按需勾选同 Case 的其他运行 |
| `/capture` 采集接入 | 复用采集 Skill 的指令生成表单 | 复制到本机 agent，再导入 JSON |

原 `/overview` 及 `/collections/:id/overview` 链接兼容。集合入口不再自动跳到第一个 Case。列表筛选存入 URL，浏览器返回或重开链接时保留筛选。Case 的 Query ID 作为主标识，显示名称作为辅助说明。

## 视觉与交互

侧栏 220px，顶部工具栏 56px。列表主体使用细分隔线与整行悬停反馈，标题为可聚焦的真实链接。列表超过 20 条分页；窄屏隐藏辅助列，核心名称与运行数量仍可见。手机将五个入口收成横向导航。

主色 #5e6ad2；正文 #25262b；次级文字 #747780；画布 #ffffff；侧栏 #f7f7f9；边界 #e9e9ee。系统 sans-serif 与 monospace 两种字体。标题 22–26px，正文 13px，辅助信息 11–12px。侧栏激活状态用低饱和底色，不用鲜艳大色块。

数字仅取真实 API，加载与失败不显示虚构的零。“已导入”只表示有轨迹，不表示评测通过。“采集进度”指已有轨迹的 Case 数，不是执行完成度或质量分。浏览、筛选和导入不触发分析。

保持 Read 蓝 / Bash 绿 / Write 橙和 Skill 的 S 标记。平台导航的强调色不改变调用方格语义。

新增功能沿用“侧栏功能 → 列表对象 → 对象详情”的结构，不把未来插件、尚未实现的设置或占位按钮塞进导航。

插件目录按能力筛选，同一包可包含渲染、分类/切片、评测。Case 内复用同一筛选状态切换方格与列表，分类产物由显式动作生成；目录浏览和视图切换不创建计算任务。
