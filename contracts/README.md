# 共享数据契约

- `schemas/`：已发布的采集输入、集合清单、评测插件和评分结果 JSON Schema。
- `schemas/plugin-v2.schema.json`、`facets-v1.schema.json`、`selection-v1.schema.json`：通用插件、分类与选择合同；实际支持范围见通用插件接入指南。
- `drafts/plugin-v2/`：保留通用插件能力的历史离线草案。
- `drafts/training-lifecycle-v1/`：查询快照、分析请求、回放、验收、数据发布与消费回执的离线草案；当前 API 未开放。
- `openapi.json`：HTTP 接口定义；`scripts/build_api_contract.py` 生成。
- 前端类型由 `npm --prefix apps/web run types` 生成至 `apps/web/src/api/generated.ts`。
- 根目录 `schemas` 和 `docs/api/openapi.json` 是兼容旧入口的相对符号链接，避免多份定义漂移。

采集器、API 和前端共用协议。前端类型仅辅助开发；导入结构与引用仍由后端校验。分析结果继续独立后触发。
