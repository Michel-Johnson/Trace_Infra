# 平台契约原型

本文对应[平台主设计](../../../docs/architecture/platform-contract-and-plugins.md)。协议 `trace-hunter-platform/1.0-draft.1`，JSON Schema Draft 2020-12。**仅作离线设计验证，线上不接受此协议。** 已上线的评测插件使用独立稳定协议，见[接入指南](../../../docs/guides/evaluation-plugins.md)。

本目录旧 plugin 对象聚焦评分；通用插件已经修订为可包含 renderer/evaluator/slicer 多个贡献点，新的离线声明见 [plugin-v2](../plugin-v2/README.md)。保留本目录原型用于已有环境与评测设计验证，不再把它当作通用插件的边界。

`platform.schema.json` 是可组合的五类对象：environment、benchmark（含 Case 定义）、run_metadata、plugin、evaluation_result。它与 trace-v2 的调用/消息/上下文模型组合，不能用 run_metadata 代替真实轨迹。引用携带 id/revision/digest；标签、版本、缺口和评测结果有明确位置。

补充：[中性导入](../../../docs/architecture/neutral-trace-import.md)不强制任务/环境/评测绑定，run_metadata 的 query_id/env_id 可为 null；单次导入 items[] 与单条轨迹 turns[] 分别表达批量和多轮。批量信封 Schema 在相邻 import-v1 目录。

```bash
.venv/bin/python scripts/build_platform_contract.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_platform_preview.py' -v
```

七份[合成样例](../../../examples/drafts/platform-v1)展示环境规范、评测集、完整覆盖声明、缺产物声明、插件描述和两种评分结果。所有 digest 使用零占位，引用内容没有提供；不是可恢复任务包、真实完整轨迹或测得性能。`run-complete-declaration` 只展示完整输入声明的形状。

离线检查包括 Schema、数量矛盾、缺口位置、版本身份绑定、插件数据要求、指标类型/范围/聚合、未评分不得填 0，以及结果不可引用其他输入。`preflight` 只演示 declared coverage 对规则的影响，不能自动证明完整度；服务端还须对照实际 Trace/附件、内容权限和版本定义。

仍需在服务实现阶段补齐：基于真实记录的 profile 检查、scope/selector/字段规则、流式导入/修订/封存、规范化摘要、版本引用解析、Job/lease/权限、查询分页/MCP 的 OpenAPI/tool schema、插件运行器与数据库迁移。本文不把这些能力包装成已实现。

首版完整性域固定，避免插件各自重新定义 token/time；新域通过协议版本或命名空间扩展注册，插件输出指标可直接新增。正文内容继续使用 trace-v2 的 source/content 引用；版本引用的 digest 与内容路径在平台内容服务中解析。
