# 后端历史数据回归基准

Python 与 Rust 后端应读取同一份固定历史输入，比较导入身份、读取结果和指标；不能把重复运行、清洗修订或人工变体计成新的历史样本。脚本只扫描文件，不连接数据库，不执行轨迹中的命令，也不触发分析。

## 已有历史数据

当前本机数据在相邻 `trace-hunter/var/`，不在 Git 中。以最新清洗版本 **1.3.0** 为主基准；每个目录都有同名 `.tar.gz`：

| 目录名 | 会话 / query | 标准记录 | 明确失败 | 有计时 / 完整区间 | 多来源片段会话 |
|---|---:|---:|---:|---:|---:|
| `doubao-work-standard-dev-50-20260911-v1.3.0` | 50 | 2,680 | 171 | 897 / 750 | 20 |
| `doubao-work-standard-20260911-v1.3.0` | 1,330 | 40,878 | 2,427 | 19,720 / 17,653 | 339 |

2026-09-12 的实际扫描结果：开发样本 22 个会话含失败，全量 637 个；两包均无冲突 run ID 或来源摘要缺失。全部标准记录都是 tool；模型请求、usage 和实际用户轮次不可由这些标准文件证明，Case 的 conversation 均为 unknown。多片段指来源组织，不能标成多轮。记录数不等于已证明独立的底层执行次数。

本机完整路径前缀为 `/Users/exburger/FutureDev/base-ai-judge/trace-hunter/var/`。较早的 `doubao-work-dev-50-20260911`、`doubao-work-chronological-20260911-v1.1.0` 以及 `standard-…v1.2.0` 仍保留，但属于同一来源的清洗修订；不要与 1.3.0 合并统计。50 个开发样本是全量的子集，不是独立留出集。

## 固定输入

使用已按仓库规则配置的 Python 环境，在平台仓库根目录执行：

```bash
.venv/bin/python scripts/build_backend_corpus_manifest.py \
  --input ../trace-hunter/var/doubao-work-standard-dev-50-20260911-v1.3.0 \
  --output var/backend-corpus/dev50-v1.3.0.json

.venv/bin/python scripts/build_backend_corpus_manifest.py \
  --input ../trace-hunter/var/doubao-work-standard-20260911-v1.3.0 \
  --output var/backend-corpus/full1330-v1.3.0.json
```

输入也可为 `.tar` / `.tar.gz`，脚本顺序读取归档，不解包。归档最外层的单一包装目录被移除，因此同一目录与其打包版得到相同清单。输出限于本仓库已忽略的 `var/`，默认权限 0600，必须使用新文件名，且不能写在输入目录内。

清单包含：

- 每个原文件的相对路径、字节数和 SHA-256，以及整个文件清单的 `inventory_digest`。压缩容器的时间戳/压缩方式不进入该摘要。
- 每份标准轨迹的原文件摘要与 canonical JSON `trace_digest`，原 run/query/env 身份、采集器版本，以及记录数、计时覆盖、顺序覆盖、工具操作/状态、来源摘要。
- Case 声明的轮次与实际观测轮次分开；后者始终为 null，因为本脚本不从原文、片段或摘要猜测用户消息。
- 重复文件、缺失来源、摘要冲突和协议错误的结构化状态，以及整个清单的 `corpus_digest`。不复制 query、标题、提示词、工具入参/返回、证据正文或校验器可能包含原文的报错。

同 run ID、同 canonical 内容的重复文件只计一次，重复路径仍保留。同 ID 不同内容使清单 invalid；不同 run ID 不因内容相似而合并。不同 query 的相似任务也不合并。

验证包括当前已发布输入 Schema 与跨记录引用规则、包内声明的导入文件大小/摘要、会话数，以及每条来源声明对应文件的实际 SHA-256。来源 JSON pointer 和字段含义没有重新验证，文件哈希一致不代表采集完整。需要完整导入验收时另用已有 `trace_import.py verify`，不能将扫描清单代替数据库回归。

## 精确复现与失败退出

```bash
.venv/bin/python scripts/build_backend_corpus_manifest.py \
  --input ../trace-hunter/var/doubao-work-standard-dev-50-20260911-v1.3.0.tar.gz \
  --check var/backend-corpus/dev50-v1.3.0.json
```

`--check` 只读重建并比较完整清单；文件内容、成员、元数据或统计口径变化都会失败。返回码 0 表示 ready 且匹配；2 表示 invalid、与基准不匹配或输入/输出路径错误。`--output` 遇到可汇总的数据问题会保存 invalid 清单并返回 2；不可安全读取的归档或路径直接失败。计时、模型请求或对话缺失属于覆盖情况，正常报告，不伪造零也不因此判输入非法。

2026-09-12 首次基准摘要：

| 清单 | corpus_digest |
|---|---|
| dev50 v1.3.0 | `a9e2d51b3f371e79f13e95d8f62fc8441a6681fbdd84bbee035fbefffba2ea81` |
| full1330 v1.3.0 | `d9238327a901bed5821b632812e4aa646c91be6c38fa38ff79d7d63304e9a572` |

如果新增文件或调整清单算法，创建新版本清单并说明差异，不覆盖原基准。清单仍含内部身份与来源路径，只保存在受限、忽略的目录，不提交 Git。

## 能证明和不能证明什么

| 历史数据可用于 | 还需独立合成或真实补充数据 |
|---|---|
| 50 条快速回归、1,330 条全量导入与分页 | 完整用户消息、多轮压缩续接 |
| run/query/env 身份、原文摘要、幂等读取 | 请求级 token、缓存计量、价格账本 |
| 部分计时、时间缺失、状态计数 | 并行模型/工具区间与完整关键路径 |
| Base 分类、失败筛选、来源片段与顺序 | 环境恢复、模型重新执行、真实业务验收 |
| 相同输入下 Python/Rust API 与统计一致性 | 训练消费回执、checkpoint 血缘和学习收益 |

人为乱序、重复导入、损坏摘要等变体适合故障回归，但必须标 `synthetic_variant` 并指向原清单，不计为新历史会话。重复跑 50 轮也只是 50 次回归执行。

运行本脚本的合成测试：

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_backend_corpus_manifest.py' -v
```

测试覆盖目录/归档一致性、同身份去重与冲突、无正文泄漏、来源缺失/改变、缺失与零计时、清单校验、危险归档路径以及精确复现失败。
