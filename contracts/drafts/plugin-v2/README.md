# 通用插件合同草案

`trace-hunter/plugin/2.0-draft.1`，对应[通用插件架构](../../../docs/architecture/general-plugin-model.md)。本目录保留离线草案；稳定 `trace-hunter/plugin/2.0` 通过 `/api/extensions` 接入，实际边界见[接入指南](../../../docs/guides/general-plugins.md)。旧 `/api/plugins` 仍接受兼容评分插件 1.0。

- `plugin.schema.json`：公共身份 + contributes[]，覆盖 renderer、evaluator、slicer(filter/classify)。能力与执行宿主分别声明，一个插件可有多个能力。
- `facets.schema.json`：分类维度、实体分配、unknown/not_applicable 与证据；没有强制分数。
- `selection.schema.json`：固定输入范围内的选择成员，允许零匹配。筛选属于交互状态。
- evaluation payload 沿用稳定 [EvaluationScore](../../schemas/evaluation-score-v1.schema.json)；view 是前端 Host 的组件生命周期与选择事件合同，定义见架构文档，并非一个任意脚本下载地址。

这些输出描述的是 payload，持久化时仍需平台添加不可变插件、贡献点、输入、配置与产物引用。input_refs 的 selection_digest 必须由平台从固定成员或已解析条件计算，不能把客户端任意声明当作授权。renderer 的包加载与隔离、远程调度、派生结果存储和服务端成员校验都尚未实现。

[样例目录](../../../examples/drafts/plugin-v2)：纯 renderer、Agent 分类 + 前端 filter / renderer 的组合、旧时间评分插件的兼容映射、部分分类与空切片。分类实体及零摘要均为合成占位；旧评分插件映射保留原实现摘要，但不是新的可安装包。

```bash
.venv/bin/python scripts/build_plugin_extension_draft.py
.venv/bin/python -m unittest discover -s tests -p 'test_plugin_extension_draft.py'
```

离线验证包含 Schema 同步、类型约束、贡献点 ID 唯一、配置合法性、权限边界、facet 引用与单/多标签约束。数据权限、输入成员真实性及完全覆盖须在正式服务中对照固定输入检查，Schema 通过不等于可部署或已运行。
