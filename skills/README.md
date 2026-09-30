# 技能目录

`skills/` 保存随版本分发给 Agent 的操作指导、适配脚本和参考资料。它帮助采集端准备标准输入，或指导外部 Agent 使用平台 API；API 的校验、存储和任务状态不依赖 Agent 是否读取了技能文本。

## 当前可用

| 技能 | 用途 | 运行依赖 |
|---|---|---|
| [trace-hunter](trace-hunter/SKILL.md) | Trace Infra 总入口与说明书；按用户目标路由 Adapter、CLI、诊断、评测和报告 Skill | 已安装相应专项 Skill；运行事实以服务 capabilities 为准 |
| [trace-hunter-adapter](trace-hunter-adapter/SKILL.md) | 识别、开发、运行和验证来源 Adapter；内含 Claude、豆包命令归档与消息 CSV 的按需参考 | 完整 Trace Hunter 仓库、Python 3.12 与仓库锁定依赖 |
| [trace-hunter-cli](trace-hunter-cli/SKILL.md) | 通过平台 CLI 导入、检索、聚合 Trace，并回读可审计证据 | Trace Hunter API、完整 checkout 与 Python 3.12 |
| [trace-eval-designer](trace-eval-designer/SKILL.md) | 澄清 Trace 评测目标，设计可评审、可执行、可版本化的 Eval Spec 与 Infra 查询计划 | Trace Hunter 能力声明与查询文档；设计阶段不要求运行服务 |
| [trace-deep-dive](trace-deep-dive/SKILL.md) | 精读少量 Trace，发现带证据、反例和验证需求的候选假设 | Trace Hunter 查询能力；只读访问固定 Trace revision |
| [trace-batch-analyzer](trace-batch-analyzer/SKILL.md) | 执行 Approved Eval Spec，完成全量分页、证据验证与聚合 | Trace Hunter 查询 API；Judge 仅在 Spec 明确要求时使用 |
| [trace-analysis-reporter](trace-analysis-reporter/SKILL.md) | 将版本化分析结果整理为可审计 Markdown、HTML 或 JSON 报告 | Eval Spec、Analysis Result 与可引用证据 |

`trace-hunter-adapter` 按 Cloudflare 式渐进披露组织：入口只包含执行漏斗和硬边界，Agent 根据真实来源按需读取 `references/`，确定性结构探测与旧豆包整理交给 `scripts/`。它不是万能猜测器；未知版本必须先建立映射契约并验证。

`trace-eval-designer` 同样采用渐进披露：入口只保留设计流程和证据不变量，按问题读取 Eval Spec、Infra 查询规划和设计模式。它只负责把用户需求固化为评测规范，不执行批量分析，也不把观察性 Cohort 差异解释为因果结果。

Trace 分析链路为：`trace-deep-dive` 从小样本提出候选假设，`trace-eval-designer` 将需求或假设固化为 Eval Spec，`trace-batch-analyzer` 执行已批准规范，`trace-analysis-reporter` 汇总结果。所有 Skill 都保留 `unknown`、禁止信息穿越，并使用稳定证据身份回链结论。

在 Agent 所在机器保留一个固定提交的完整 checkout，按[依赖规则](../docs/security/dependencies.md)准备 `.venv` 后，在仓库根目录安装发现入口：

```bash
.venv/bin/python scripts/install_collector_skill.py --skill trace-hunter
.venv/bin/python scripts/install_collector_skill.py --skill trace-hunter-adapter
.venv/bin/python scripts/install_collector_skill.py --skill trace-hunter-cli
.venv/bin/python scripts/install_collector_skill.py --skill trace-eval-designer
.venv/bin/python scripts/install_collector_skill.py --skill trace-deep-dive
.venv/bin/python scripts/install_collector_skill.py --skill trace-batch-analyzer
.venv/bin/python scripts/install_collector_skill.py --skill trace-analysis-reporter
.venv/bin/python skills/trace-hunter-adapter/scripts/prepare.py --help
.venv/bin/python skills/trace-hunter-adapter/scripts/inspect_source.py --help

# Claude Code：改为其个人 skills 目录（选择与目标 agent 对应的一条）
.venv/bin/python scripts/install_collector_skill.py --skill trace-hunter-adapter --skills-dir "$HOME/.claude/skills"
```

安装后，Codex 使用 `$trace-hunter` 作为总入口，并按任务进入 `$trace-hunter-adapter` / `$trace-hunter-cli` / 分析评测 Skills；Claude Code 使用对应的斜杠命令。安装器建立本地符号链接，默认位于 `$CODEX_HOME/skills`（未设置时为 `~/.codex/skills`），保留已有不同内容。仓库路径必须持续可用；命令不会安装 Langfuse、启动采集、上传数据或自动导入平台。

## 与服务和镜像的关系

| 发布单元 | 职责与技能的关系 |
|---|---|
| Web 镜像 | Caddy 提供已构建前端和统一 `/api` 网关；不执行技能 |
| API 镜像 | 对外接口与协议校验，调用 Backend 业务模块 |
| Backend 基础镜像 | 共享核心、锁定依赖及迁移；为 API、Plugin 和 agent-tools 提供构建基础 |
| Plugin 镜像 | 官方计算插件的 Worker；前端插件仍由 Web 加载，外部插件通过 API 接入 |
| PostgreSQL 镜像 | 独立存储与持久卷；技能无需数据库凭据 |
| 可选 agent-tools 镜像 | 打包 `/app/skills`、转换脚本与 MCP 桥，供 Agent 按命令调用 |
| 外部 Agent / 采集环境 | 持有技能和它声明的运行依赖；按任务需要使用 HTTP 或启动本地 MCP 桥 |

Web、API、Plugin、PostgreSQL 是常驻服务；Backend 同时用于一次性迁移，agent-tools 是可选客户端。构建与调用见[容器部署](../deploy/containers/README.md)。已有 job-bound MCP 桥是 [scripts/evaluation_mcp.py](../scripts/evaluation_mcp.py)，由 Agent 通过工具镜像或完整仓库的锁定 Python 环境启动；[对外接入](../docs/api/external-access.md)说明凭据和调用顺序。

## 版本责任

- 技能指导与支持脚本按仓库提交一起分发，发布记录固定提交和文件摘要。当前没有单独的技能包发布器；新增正式技能按技能创建流程审查，不只增加一段提示词。
- 适配器独立声明转换版本；当前豆包命令归档适配器为 `1.3.0`，消息级 CSV 适配器为 `1.0.0`，输出记录 `collector.version`，包清单记录 `converter_version`。转换语义变化需要新版本和可复查来源，不能覆盖既有导入事实。
- 输入 Schema、HTTP 和插件 Manifest/产物各自版本化，来源是 [contracts](../contracts/README.md)。技能说明引用这些合同，不维护另一套字段定义；兼容范围要与实际脚本验证一致。
- 平台分类/评分插件的版本独立于采集适配器。技能可指导 Agent 运行方法，平台只接受符合固定输入、配置和输出合同的结果；浏览和导入不触发计算。

以后新增客户端适配器，先声明支持的来源格式、运行依赖、输出协议版本、来源保留方式和验证入口。能够交付“标准文件 + 可回查来源 + 转换版本”，就能通过中性的导入 API 接入，无需修改核心协议来识别技能名称。
