# Trace Hunter Skill 系统设计

## 目标

让 Agent 在面对“导入一种新 Trace”“查询已有 Trace”“运行评测”时，先选择正确工作流，再按平台能力执行，而不是依赖一个不断膨胀的万能 Skill 或万能 Adapter。

Skill 只负责路由、决策顺序和平台用法。数据正确性仍由 Adapter、Schema、API 权限和确定性校验程序保证；Skill 不能把缺失证据补成事实。

## 参考原则

本设计借鉴 Cloudflare Skills 的四点做法：

1. 总入口是 Router，只回答“该用哪个能力”，不复制所有专项知识。
2. 专项 Skill 按 Agent 的真实执行顺序组织，而不是按知识目录堆砌。
3. 先读取当前能力和输入事实，再使用内置知识，避免依赖过期记忆。
4. Agent 负责编排和语义判断；哈希、格式探测、校验、批量执行等机械步骤交给确定性脚本。

参考：[Cloudflare Skills](https://github.com/cloudflare/skills)、[Cloudflare Router Skill](https://github.com/cloudflare/skills/blob/main/skills/cloudflare/SKILL.md)、[Agents SDK Skill](https://github.com/cloudflare/skills/blob/main/skills/agents-sdk/SKILL.md)、[Turnstile Workflow Skill](https://github.com/cloudflare/skills/blob/main/skills/turnstile-spin/SKILL.md)。

## 总体结构

```text
用户任务
   │
   ▼
trace-hunter                    只做意图识别与路由
   ├── 原始 Trace、已知来源、新格式 ─► trace-hunter-adapter
   ├── 导入、搜索、统计、证据回溯 ──► trace-hunter-cli
   └── 评测、判分、防信息穿越 ─────► trace-hunter-eval（后续实现）
```

当前 `trace-hunter` Router 已落地，作为整套 Infra 的入口说明书；它只负责能力解释和任务路由，具体执行仍由专项 Skill 完成。

Router 可以同时选择两个 Skill。例如“为新格式增加 Adapter，然后导入并查询”先使用 `trace-hunter-adapter`，通过验收后再使用 `trace-hunter-cli`。不能跳过 Adapter 验收直接导入。

## 建议目录

```text
skills/
├── trace-hunter/                     # 总路由
│   ├── SKILL.md
│   └── references/
│       └── capability-map.md
├── trace-hunter-adapter/             # Adapter 开发与验证
│   ├── SKILL.md
│   ├── references/
│   │   ├── adapter-development.md
│   │   ├── evidence-boundaries.md
│   │   ├── claude.md
│   │   ├── doubao-command-corpus.md
│   │   └── doubao-message-csv.md
│   └── scripts/
│       ├── inspect_source.py
│       └── prepare.py
├── trace-hunter-cli/                 # 平台操作与分析
│   ├── SKILL.md
│   └── references/
│       ├── cli.md
│       ├── query-model.md
│       ├── analysis-recipes.md
│       └── errors.md
└── trace-hunter-eval/                # 暂不创建；未来实现
```

平台操作 Skill 已统一命名为 `trace-hunter-cli`。旧的 `trace-hunter-workbench` 目录不再保留，避免两个完整 CLI Skill 长期漂移。

## 1. 总路由 Skill：trace-hunter

### 职责

- 根据用户目标选择最少的专项 Skill。
- 先判断任务属于“新格式接入”还是“操作已有平台数据”。
- 明确多个 Skill 的执行顺序。
- 对当前系统不支持的能力给出缺口，不自行发明命令或字段。

### 不负责

- 不包含完整 CLI 手册。
- 不定义某个厂商的字段映射。
- 不执行评测判分。
- 不把自然语言 Trace 猜成某个已注册格式。

### 路由表

| 用户目标 | 加载 Skill | 说明 |
|---|---|---|
| “这批原始 Trace 能否导入” | `trace-hunter-adapter` | 先识别格式与语义缺口 |
| “增加/修复某厂商 Adapter” | `trace-hunter-adapter` | 开发、校验、注册 Adapter |
| “搜索 Span/正文/Skill” | `trace-hunter-cli` | 查询已有投影 |
| “分析耗时/Funnel/Cohort” | `trace-hunter-cli` | 组合结构化与文本接口 |
| “导入 Claude/Doubao 已知格式” | `trace-hunter-adapter` + `trace-hunter-cli` | Adapter 内选择对应来源 reference，准备完成后再入库 |
| “运行 benchmark/评分” | `trace-hunter-eval` | 未实现前明确停止 |

## 2. Adapter Skill：trace-hunter-adapter

这是“生产可靠 Adapter”的 Skill，不是运行时万能解析器。

### 执行漏斗

```text
确认输入来源与版本
  → 对样例做只读结构探测
  → 检查是否已有精确 Adapter
  → 写映射契约与 unknown 边界
  → 实现 Adapter
  → Schema、source_refs、哈希和语义校验
  → 先测 2 条，再测全量
  → 注册格式并输出语义损失报告
```

### 必须坚持的边界

- 格式识别只能来自显式格式名或稳定结构签名；相似字段不能自动降级为另一个 Adapter。
- 一次真实执行对应一个 Span；proposal、retry、context、depends_on 只有来源明确证明时才生成。
- `null`、0、空字符串和字段缺失必须分别保留。
- input、output、message、context 分字段保存，不能合成一个不可追溯文本块。
- 每个对象必须带可解析的 `source_refs`；来源哈希按该格式公开契约验证。
- Adapter 不得读取 Gold Truth、评分答案或其他只属于 Judge 的文件。
- 新格式必须使用独立名称和版本；不能把厂商特例悄悄塞进通用 Adapter。

### Agent 与脚本分工

Agent 负责：判断事件语义、选择 Span 粒度、确定哪些信息必须保持 unknown、解释语义损失。

脚本负责：统计字段形状、生成稳定样例摘要、复算哈希、逐字段比较原文和 V2、检查对象数量与 `source_refs`、输出机器可读报告。脚本只检查确定性事实，不替 Agent 猜业务语义。

### 交付物

- Adapter 实现与注册项。
- 最小真实形状 fixture 和边界 fixture。
- 映射表、已知缺失、禁止推导项。
- prepare/check 报告及回归测试。
- 2 条试转换和全量转换结果；不提交原始 Trace。

## 3. CLI Skill：trace-hunter-cli

原 `trace-hunter-workbench` 的平台操作能力已迁入此 Skill，并按真实查询顺序重排。

### 执行漏斗

```text
解析目标与证据要求
  → 读取 URL、Project 与认证配置
  → 调 capabilities 获取当前服务能力
  → 先用结构化字段缩小范围
  → 再做 literal / regex 正文搜索
  → 必要时遍历关系、Funnel 或 Run Set
  → 回到 source_refs / trace content 复核
  → 报告范围、分页、unknown 与证据
```

### 路由原则

- Trace 元数据与索引状态：`trace query`。
- Span、Skill、状态、耗时：`span`。
- 正文与命中片段：`search`。
- 结构化条件 + 正文 + 关系 + 聚合：`object`。
- 有/无某行为的集合：`run-set`。
- 多阶段到达与中断：`funnel`。
- 异常耗时：`duration`，需要分位数时与 `object` 或分页 Span 组合。

### 关键边界

- CLI Skill 不直接查询数据库，不绕过 API 权限。
- 先读取 capabilities，不假定部署拥有仓库最新接口。
- literal / regex 只做候选召回；关键结论必须复核结构化字段或原文。Trigram 是索引实现，不是公开查询模式。
- `next_source` 只证明来源顺序，不证明因果或同一并行分支。
- `text_state=truncated`、缺失耗时、未知状态必须出现在结论限制中。
- 导入、浏览和查询不得自动触发分析或评测。

## 4. Adapter 内的来源参考

Claude 和 Doubao 不再各自暴露独立 Skill。`trace-hunter-adapter` 先识别来源，再按需加载对应 reference；来源版本变化仍走同一个“映射契约 → 实现 → 验证”漏斗，避免多个 Skill 复制协议和校验规则。

Adapter 完成 prepare/check 后，才把标准 V2 交给 `trace-hunter-cli` 导入。来源 reference 只保存该格式特有的证据边界和命令，不复制 Adapter 通用规则或查询手册。

## 5. Eval Skill：trace-hunter-eval（后续）

本阶段只保留职责边界，不创建 Skill 文件。

未来 Eval Skill 负责：验证 evaluator-only 入口、防信息穿越、读取公开 Query 而隔离 Ground Truth、运行查询、保存请求响应、调用 Judge、计算得分和输出报告。

Eval Skill 不负责修 Adapter、修改 Gold、查询 analysis scope 兜底或直接访问数据库。评测发现数据映射问题时停止并转交 `trace-hunter-adapter`，修复后从新 Project 重新运行。

## 共享约定

### Retrieval-first

Router 和专项 Skill 都不能把仓库内文档当成正在运行服务的事实。CLI 先读 capabilities；Adapter 先读真实样例和当前 Schema；涉及版本、字段或限制时优先读取仓库当前合同。

### 渐进加载

`SKILL.md` 只保留触发条件、执行漏斗和硬边界。命令全集、字段表、映射细节和错误恢复放进 references；只有当前步骤需要时才读取。重复机械逻辑放进 scripts，不让每个 Agent 重新实现。

### 安全与可复现

- Skill 是操作说明，不是权限边界；API 与文件权限仍是最终约束。
- 远程 Skill 必须固定版本或摘要，不能在评测中加载未固定的在线指令。
- 原始 Trace、数据库、认证信息和评测 Ground Truth 不进入 Skill 包或 Git。
- 每个 Skill 的最终结论都必须能追溯到命令输出、报告或 source refs。

## 实施顺序

1. 新建轻量 `trace-hunter` Router。
2. 新建 `trace-hunter-adapter`，合并 Claude/Doubao 来源说明，覆盖当前 `sample10-raw-v1` 缺口，并提供确定性检查脚本。
3. 将 `trace-hunter-workbench` 改名为 `trace-hunter-cli`，并同步安装与调用入口。
4. 让一个独立 Agent 分别完成“新格式接入”和“已有 Trace 分析”测试，检查是否选对 Skill、是否越权猜测。
5. 实际评测链路稳定后再实现 `trace-hunter-eval`。

## 验收标准

- Router 对典型请求只加载必要 Skill，不复制专项说明。
- Adapter Skill 面对未知格式会停止或创建新 Adapter，不会误用相似格式。
- Adapter 验证能发现对象丢失、字段串位、哈希不符、假 Retry/Context 和 `null` 变 0。
- CLI Skill 能根据 capabilities 选择接口，返回分页范围和可追溯证据。
- 两个独立 Agent 测试中，一个完成 Adapter 接入，一个完成查询分析；二者均不读取数据库或 Ground Truth。
- Eval Skill 未实现时，Router 对评测请求明确报告缺失，不用普通 analysis 查询冒充评测。
