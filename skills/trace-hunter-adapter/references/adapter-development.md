# Adapter 开发与注册

## 先读当前实现

不要维护第二套协议说明。以当前 checkout 为准，按顺序读取：

1. `scripts/trace_agent.py describe`：已注册格式、绑定要求、目标 Schema 和检查能力。
2. `src/trace_hunter/interop/service.py`：prepare/check 服务边界。
3. `src/trace_hunter/interop/service.py` 与同目录的来源模块：格式探测、转换和注册方式。
4. `contracts/schemas/trace-v2-storage-v2.schema.json`：实际可接收的 v2 storage profile。
5. 对应 Adapter 测试与 fixture：当前行为事实。

历史 `claude-export` 是特定实验文件，不代表任意 Claude JSONL；`doubao-export` 与 `doubao-turn-export` 也只接受各自稳定结构。新增来源或破坏性结构变化应使用新的格式名和转换版本。

## 何时修复，何时新增

- 输入符合已注册格式的稳定签名，但转换报错、对象丢失、字段落错、`source_refs` 无效，或导入后无法按应有的 Skill/Tool/正文检索：修复现有 Adapter，并增加该真实形状的回归 fixture。
- 来源版本、事件边界或稳定签名已经变化，旧 Adapter 的契约不再成立：注册新的明确格式/版本，不在旧 Adapter 中静默兼容另一种语义。
- 只是来源缺少信息：保留 unknown 和 coverage 缺口，不通过开发 Adapter 伪造模型请求、Context、时间、token 或关系。

不要把“Schema 校验通过”当作修复完成。必须用来源中可核对的代表对象回读：例如 Skill 名称与 action、Tool 输入/输出、正文搜索片段、重试或 proposal→execution 关系。

## 最终输出合同

当前权威目标由 `scripts/trace_agent.py describe` 返回；目前是 `trace-hunter/2.0-draft.2`，Schema 为 `contracts/schemas/trace-v2-storage-v2.schema.json`。Adapter 返回的 `trace.json` 至少包含 `schema_version`、`document`、`run`、`capture`、`sources`、`segments` 和 `spans`，其他 v2 对象按来源证据生成。

`source.json` 保留原始字节，`trace.json` 是规范化 v2，`report.json` 保存映射、能力和语义问题，`manifest.json` 绑定文件哈希。正式导入 API 接收原始来源并在服务端运行同一个 Adapter，最终写入的仍是上述 v2 storage profile，而不是厂商 JSON 或 Skill 自定义格式。下载的 Skill 包不包含此转换器源码；开发/修复需要完整 checkout（仓库地址 `git@code.byted.org:bitable/trace-hunter`）。没有仓库访问权时报告缺口，不在下载包里虚构转换能力。交付的新 Adapter 必须进入服务端注册表后才可由线上 API 调用。

## 映射契约

实现前写清：来源版本/签名、归组单位、事件边界、稳定 ID 组成、顺序与时间单位、正文与附件位置、token 是增量还是累计、状态证据、Skill 名称与 `load`/`invoke` 语法、metadata 载体、重试/提案/执行关系、截断标记、unknown 规则和原件哈希算法。

然后定义来源字段到 v2 对象与字段的落点。原始字节完整保留只是底线；来源中每个已有字段、事件、正文和附件必须有类型/数值不变的规范字段、有命名空间的扩展字段，或指向不可变内容的精确来源引用，并能从转换产物回读。Agent 审阅整个来源格式的字段和可选分支，记录各自落点、是否可查询及语义不确定性；发现未映射信息就修复 Adapter。只有原件备份、粗粒度根指针或 `unsupported` 警告，不足以认定零信息损失。目标 Schema 容纳不了时，扩展合同或停止交付；不得为过 Schema 丢弃或伪造。

“零损失”仅针对来源实际提供的内容：原件本身缺失、截断或外部附件未提供时保留该状态，不推断不存在的事实，也不把这类输入宣称为完整 Trace。

## 实现与验证

- 复用 interop 公共解析、错误结构、ID 与哈希工具；不要新建旁路导入器。
- 在 `src/trace_hunter/interop/` 增加或修改来源模块，并在 `service.py::ADAPTERS` 注册明确格式名；更新 `describe` 可见性。
- Adapter 不联网、不执行上传内容、不覆盖输出。
- 增加最小真实形状 fixture（脱敏/合成）和边界 fixture：乱序、缺时间、null/0/空串、重复快照、重试、截断、ID 冲突。
- Agent 审阅字段映射和来源格式各分支，并核对原始字节哈希、对象计数、关系端点、`source_refs`、正文与附件引用、转换幂等和稳定 ID；发现信息未映射或无法回读时，无损验收失败。
- `check --full` 验证 Schema、跨对象约束、文件哈希和已有 source pointer，不能替代 Agent 的信息完整性审核。字段清单或差异脚本可辅助审核，但不要求另开发自动证明器，也不能拿该命令的 `ok=true` 冒充审核结论。
- 全量转换后按不同格式变体、长度、状态和边界抽 10 条 Trace（不足 10 条则全查），由 Agent 逐项对照原文、v2 与映射说明。修复后重新审阅字段映射并抽样，不能只复查失败样本。
- 通过正式 API 导入隔离测试项目并回读对象和搜索命中，至少比较成功、错误、Skill、metadata、截断/缺失事实；来源不存在该类事实时明确记录不适用。抽样通过不代表全量转换正确，正式导入仍需检查每条记录的转换和投影状态。

来源协议变化时提升 Adapter/格式版本。Adapter 实现升级本身不等于创建新的 Trace revision；既有导入事实不得覆盖，重处理策略由平台版本政策另行决定。
