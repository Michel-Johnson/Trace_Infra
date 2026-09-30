---
name: trace-analysis-reporter
description: 将版本化 Trace Eval Spec、批量 Analysis Result 和逐条 DeepDiveResult JSONL 整理为可审计的 Markdown、HTML 或结构化分析报告。用于面向研发、产品或评测人员解释结果、证据、覆盖率和限制；不用于重新执行评测或在报告阶段生成未经验证的新结论。
---

# Trace Analysis Reporter

## 版本预检

版本页：[查看最新 Skill](http://10.37.24.3:8766/skills.html)；机器接口：[查询版本](http://10.37.24.3:8766/api/skills/manifest)；[下载官方包](http://10.37.24.3:8766/api/skills/archive)。仅在服务器的 tracehunter-agent 隔离 worker 中，若公网入口连接拒绝或超时，改用 [worker 版本接口](http://127.0.0.1:8767/api/skills/manifest) 和 [worker 下载接口](http://127.0.0.1:8767/api/skills/archive)；HTTP 错误不能靠切换地址绕过。

每次使用前确认接口返回 HTTP 200 且有本 Skill 的版本条目，下载同一入口的官方包并逐文件对比本 Skill。空输出、非零退出码或无法读取压缩包都不是通过；记录检查结果。安装态有更新时保留用户改动并更新、校验；仓库开发态只报告差异，不用线上包覆盖源码。无法检查或更新时停止。

把已完成的 Trace 分析转化为读者可理解、审阅者可复查的报告。报告层不改变分析口径。

讨论报告结构或解释既有结果不创建任务。用户准备执行独立的报告产出 workflow 时，先说明任务名称、输入版本、目标读者、阶段和完成条件，问是否创建任务并开始；同意后按 Trace Hunter CLI 创建或沿用任务，按“校验输入→撰写→证据复核→交付”更新进度和终态。若报告属于已创建的综合任务，复用该任务而非再建一条。

## 工作流

1. 确认输入来源与版本：Eval Spec、Analysis Result、DeepDiveResult JSONL、必要证据和目标读者。输入要求见[报告合同](references/report-contract.md)。
2. 校验 Spec、批量 Result 和逐条 DeepDiveResult 的身份、revision、版本与覆盖数据是否一致；Draft 下的个案判断只能用于校准或探索展示。不一致时停止合并并列出冲突。
3. 将内容分为事实、统计结果、解释、假设和建议。解释边界见[解释规范](references/interpretation-policy.md)。
4. 先写执行摘要，再写范围与方法、关键结果、典型案例、未知与限制、建议和证据索引。
5. 输出 Markdown、HTML 或结构化 JSON。HTML 要求见[HTML 报告](references/html-report.md)。
6. 检查每个关键结论是否有结果字段或稳定证据引用，并确认没有因措辞压缩而改变 pass/fail/unknown 或相关性语义。

## 核心不变量

- 不重新查询全量 Trace 来填补报告空白；需要新分析时退回对应 Skill。
- 不把 Draft Spec 下的 Deep Dive 暂定判断或个案 hypothesis 写成已验证总体规律，不把观察性差异写成因果。
- 不隐藏 unknown、excluded、truncated、状态/Token/时间覆盖率和未匹配 Cohort。
- 不把基础设施错误、数据缺失或 Judge 解析失败写成 Agent 质量失败。
- 典型案例用于解释，不替代总体指标；异常个案不能代表分布。
- 每个数字保留单位、分母、样本范围和模板版本。
- Trace 正文默认最小引用，避免泄露凭据、个人信息和无关内容。

## 输出原则

结论按重要性排序，而不是按分析执行顺序罗列。建议必须对应已验证发现，并说明预期影响和验证方式；证据不足时写成后续问题，不给确定修复结论。
