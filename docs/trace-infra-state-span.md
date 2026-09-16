# Trace infra：state / span 讨论笔记

## 结论

`state` + `span` 这个拆法成立，而且正好对上你要做的实验。

`state` 是一个可恢复、可复制的检查点。`span` 是两个 `state` 之间实际发生过的短轨迹。原轨迹是一条 span 链；实验是在同一个 `state` 上再长出新的 span，而不是改掉旧 span。

OpenTelemetry 里的 span 是「一次计时操作」（可嵌套）。这里的 span 是「两个状态之间的短轨迹」。名字可以沿用，但文档里必须写清定义，不要按 OTel 语义实现。

## 上次说的 action 是什么

在本仓库里，一次 action 就是 `Episode.act(name, params)`。

模型先输出 JSON，例如 `{"action": "run_all", "params": {"input": "(]"}}`。环境再执行这一步，返回统一 envelope（status、cost、observation）。`trajectory.jsonl` 的每一行就是一次 action 的记录。

真实 Executable World 里，action 还可以是容器里的 shell 命令。本质相同：模型决定调用什么，环境执行，环境状态因此改变。

所以：

- 「命令错误」= 这一步的 `name` / `params`（或 shell 字符串）是错的。
- 「方向错误」= 连续若干步 action 组成的序列整体不是你要的路径。

你要的实验，重点通常不是把这些 action 再执行一遍，而是：停在那次错误 **之前** 的 `state`，把当时要送给模型的输入固定住，再调模型 N 次。

## 两种能力怎么同时支持

不要做成一个开关叫「回放」。做成对同一个 `state` 的两种操作：

1. **重放已记录 span**：按记录把 action 再送给环境。用来验证记录是否完整、环境是否确定。
2. **从 state 分叉**：复制这份环境，再调模型，用新输出生成新 span。用来做你说的偶然 / 必然实验。

第 2 种才是主场景。N 次实验必须有 N 份 `state` 副本。第一份执行一旦改了文件系统、budget、内部变量，第二份就不是「完全相同的环境」。

一次实验最少要固定这些东西：

- 环境：`task_id`、`seed`、budget、Task 内部状态；真实系统还要包括容器文件系统和进程。
- 模型输入：当时那一轮的 `system + transcript`（或等价 prompt）。
- 采样设置：model id、temperature、seed（如果供应商提供）。

模型输出属于 span，不属于 state。state 里不要存「当时模型说了什么」，否则分叉时你会把旧答案又喂回去。

## 粒度

你还没定粒度，可以先用一个能做实验的最小切法，不必一次定死全部层级。

建议默认在「下一次模型调用之前」切一个 `state`。此时环境已执行完上一轮 action，下一轮模型输入也已确定。这就是你要重复调模型的位置。

更细（token / logits）只有在你要分析采样过程时才需要。更粗（只在 submit 后切）无法停在「那条错误命令」前面。

span 默认覆盖「一次模型调用 + 它引起的环境 action」。如果一次模型调用打出多个 tool call，可以先仍算一条 span，内部用事件列表保存每一步；等有数据再决定要不要拆。

## 开源对照（按接近程度）

目标匹配度高的，优先看这几个，不要从零发明 fork 语义。

**LangGraph checkpoint / time travel**  
https://docs.langchain.com/oss/python/langgraph/use-time-travel  

checkpoint = 你的 state。官方把 replay 和 fork 分成两个操作：replay 从旧 checkpoint 往后重新执行；fork 先 `update_state` 再继续，原历史保留。语义最接近，但保存的是图状态，不是容器。

**Inspect AI checkpointing**  
https://inspect.aisi.org.uk/checkpointing.html  

resume 时恢复 sandbox、store、对话记录，再让 agent 继续。对「相同环境 + 相同对话再跑」对齐，主用途是崩溃续跑，不是 N 路对照实验，但恢复清单可以直接抄。

**OpenHands**  
轨迹 replay：`openhands/controller/replay.py`，按记录把 Action 再执行一遍，不恢复容器快照。容器 snapshot/restore 是另一条线（issue 6163）。说明「重放 action」和「恢复相同环境」是两件事，不能混成一个 API。

**环境复制（真实容器）**  

- [forkd](https://github.com/deeplethe/forkd)：Firecracker 快照 + 运行中 BRANCH。  
- [AgentENV](https://github.com/kvcache-ai/AgentENV)：环境 snapshot / fork。  

example 任务不必上微 VM：`load_task(id, seed)` 再执行 action 前缀就能还原。真实 EW（shell、GPU、墙钟）必须走快照 fork，只重放 jsonl 不够。

**Shepherd**（https://arxiv.org/abs/2605.10913）把 agent 与环境做成可 checkout 的执行轨迹，场景很像，但项目新、实现要自己核对，适合当论文对照，不适合当唯一地基。

建议：fork API 抄 LangGraph；恢复内容抄 Inspect；action 重放抄 OpenHands；真实环境复制用快照，而不是重放日志。

## 需要你定的点

1. `state` 是否包含模型输入（建议包含）和模型输出（建议不包含）。
2. 默认粒度是否接受「每次模型调用前一个 state」。
3. 第一期 example engine 是否只做「seed + action 前缀还原」，真实容器快照放到下一期。
4. 名字继续用 span，还是改成 `segment`，以避开 OTel。
