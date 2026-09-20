# Dressage 调研

对照源：https://github.com/Accio-Lab/Dressage  
读到的版本：`3e3142fe`（2026-08-27，README 写「Scalable RL for Any Agent and Sandbox」）  
Apache-2.0。底座是 [slime](https://github.com/THUDM/slime) 的 git submodule，不 fork 上游。

这是 research 分支的第一份笔记。本仓库已经定了：`state` 是可恢复、可复制的检查点；`span` 是两个 `state` 之间实际发生过的短轨迹；真实环境必须冻住文件和进程，不能只重放 jsonl。Dressage 解决的是另一件事。

---

## 1. 它是什么

Dressage 是 **agentic RL 训练框架**。白盒 Python 工具循环和黑盒 HTTP agent（`opencode`、`openclaw`、`claude_code`、`codex`）都走同一条路径：rollout → 代理记录 token → 转成 slime 训练样本。

它要保证的是：

- 训练证据精确到 token：`token_id`、`logprob`、`loss_mask`、`token_version`、MoE 的 `token_expert`
- 长对话不因重新 tokenize 而漂移（TITO）
- 异步训练时，rollout 策略和当前策略不要差太远（staleness）
- 隔离环境能换，agent 语义不用改

它 **不是** sandbox 控制面。没有把一台正在跑的环境冻住、再解冻成另一台、再从同一点分出 N 份副本。本仓库第 0 阶段那四条写接口（`CommitState` / `RestoreState` / `ReplaySpan` / `Branch`）在 Dressage 里对不上。

---

## 2. 怎么拆层

README 的心智模型是三条正交轴，slime 在底下：

```text
Dressage  : 代理、轨迹、训练样本转换
Paddock   : 交互语义（whitebox Python 循环 vs blackbox HTTP agent）
Sandbox   : 放置与隔离（local bubblewrap vs 远程 E2B）
slime     : Megatron 训练、Ray rollout、SGLang 推理
```

paddock 模式和 sandbox 提供方可以自由组合。工厂用环境变量接线，启动时检查是否配错（例如 blackbox paddock 配了 `command_only` 池会直接报错）。

对本仓库有用的是这个拆法本身：

- **交互语义**（agent 调什么、怎么调）不要写进 VMM。
- **放置与隔离**（这台环境跑在哪、怎么隔离）不要知道 agent 协议。
- **轨迹采集**（记了什么、给谁用）不要和环境 dump 塞进同一张表。

本仓库已经按这个方向走：`episode` 和 `firecracker` 共用同一套字段；调用方不直接碰 VMM。Dressage 把同一原则做到了「agent 类型」和「sandbox 提供方」两轴。

---

## 3. 沙箱：租约，不是检查点

`SandboxProvider` 的契约在 `dressage/sandbox/provider.py`，数据类型在 `types.py`。提供方只拥有生命周期和底层能力，不准知道 blackbox 的 `register_agent` / `call_agent`。

实际方法：

| 方法 | 做什么 |
|---|---|
| `create(SandboxSpec) → SandboxLease` | 为 **一条 trajectory** 拿一份租约 |
| `terminate(lease)` | 释放租约 |
| `get_public_url` | 暴露服务端口（blackbox HTTP） |
| `run_command` / `read_file` / `write_file` | 命令和文件 |

没有 snapshot、restore、fork、rollback。E2B 实现（`dressage/sandbox/remote/e2b/provider.py`）是 `AsyncSandbox.create` 和 `kill`。本地 bubblewrap 是 Ray 管的槽位池：`create` 分配空槽，`terminate` 清文件系统、必要时重启槽里的 BlackboxServer，槽回到池里。

两种提供方：

| 提供方 | 隔离 | 生命周期 |
|---|---|---|
| `local_bwrap` | bubblewrap 命名空间，无特权 | 预创建槽位，supervisor 探活，坏了重启槽 |
| `e2b` | E2B 云沙箱，模板镜像 | API 创建 / kill，弹性扩 |

两种池模式：`blackbox`（槽里跑 BlackboxServer）和 `command_only`（只有 shell / 文件）。白盒工具走后者。

可选把 `home` / `work` / `runtime` / `tmp` 打成会话归档，带 TTL。这是失败后取证，不是可恢复 `state`。归档不能 `RestoreState`。

和本仓库的差别：

| | Dressage | Trace Infra |
|---|---|---|
| 活句柄 | `SandboxLease`，绑一条 trajectory | `sandbox_id`，进程重启作废 |
| 持久点 | 没有。terminate 就没了 | 只有 `state_id` |
| 恢复 | 再 `create` 一份新环境 | `RestoreState`：文件还在，进程还在 |
| 分叉 | 没有。N 路采样 = N 份全新租约 | `Branch(n)`：同一份 state 的 N 个副本 |
| 磁盘 / 内存 | 不建模 | snapfile + memfile + rootfs + header |

选型文档里已经写过：E2B 的 pause / snapshot / fork 是产品层能力，Dressage 作为训练框架只用了 E2B 的「按模板开一台、用完杀掉」。它没有把 E2B 的检查点接到自己的 API 上。本仓库若接 E2B 或 CubeSandbox，接的是检查点，不是 Dressage 这层租约。

---

## 4. pause / resume 不是冻环境

`BlackboxPaddock.pause` / `resume` 的默认 `reason` 是 `weight_update`。它协调的是代理里的 `GenerationController`：在 **token 边界** 打断正在进行的 SGLang 生成，权重更新后再从部分输出接着生成（partial rollout）。

这解决的是异步 RL 吞吐：长轨迹不要因为一次权重更新整段丢掉。`docs/staleness.md` 还限制一条部分轨迹最多跨几个权重版本，避免前缀和环境状态都是旧策略、只在最后几个 token 上算 loss。

本仓库的冻住是另一件事：CPU、内存、磁盘、正在跑的 `sleep` 都停在当时。Dressage 的 pause 不碰 guest 进程，也不写快照文件。看到 `pause` / `resume` 不要当成 `CommitState` / `RestoreState`。

---

## 5. 轨迹：token 和训练样本，不是 action span

Dressage 一条路径：

```text
代理 finalize_session → trajectory/read → expand_segments_to_samples → list[Sample]
```

粒度：

| 名字 | 含义 |
|---|---|
| session | 一条完整 agent 轨迹，从第一条 user 到结束 |
| step | 一次 `/v1/chat/completions` |
| segment | 历史被改写、工具 schema 变了、或 TITO 失败时切开的可训练片段 |

每一步记下：完整 messages、prompt/response token id、逐 token logprob、权重版本、loss mask、可选 MoE expert id。TITO 模式下还有 `concat_token_ids` 一类增量字段。

**TITO（Token-In-Token-Out）**：每轮只 encode 新追加的 delta，再把 token id 拼上去。禁止改写已有前缀。一旦 agent 压缩历史或改工具列表，当前 segment 封口，新 segment 从干净状态开始。目的是避免「同一段字重新 tokenize 得到不同 id」，让 rollout 时的 logprob 和训练时的 token 对得上。

多 segment 时，每个 segment 都变成一条 slime `Sample`，共享 `rollout_id` / `parent_traj_id`，锚点 segment 的终局 advantage 广播给兄弟姐妹。这是为了 GRPO 公平，不是为了在错误命令前停住环境。

和本仓库对照：

| Dressage | Trace Infra | 能不能当同一个东西 |
|---|---|---|
| session | run | 近似：一次实验跑 |
| step（一次 LLM 调用） | 一条 span 里可能有多次 `act` | 不能。这边默认 span 覆盖「一次模型调用 + 它引起的环境 action」 |
| segment（训练切开） | 无对应物 | 不能。名字容易和 span 混 |
| token / logprob | 不进 state，也不进 span 事件 | Dressage 的核心，本仓库第一期不需要 |
| messages 快照 | `prompt` 挂在 state 上 | 方向接近：下一轮模型输入属于检查点，不属于旧输出 |
| 环境 observation | span 事件里的完整 `observation` | Dressage 几乎不把它当重放契约 |

本仓库写明：模型输出不属于 span 事件；prompt 挂在 state 上。Dressage 把模型输出（token、logprob）当训练主数据，环境只是 rollout 时的执行场所。两边都对，服务的实验不同。

Harbor 那条线（`docs/harbor.md`）把任务、环境、verifier、reward 交给 Harbor，Dressage 只截模型请求、留下可训练轨迹。ATIF / Harbor trial 资产是评测和 SFT 导出，不是 `ReplaySpan`。verifier 在轨迹 finalize 之后、sandbox teardown 之前跑，然后释放租约。环境不保留。

---

## 6. 存储：大字段离开控制面

代理默认把 finalize 后的 segment 放在进程内 `trajectory_store`。后来接 [TransferQueue](https://github.com/Ascend/TransferQueue)，把 `logprobs` 和 R3 `routed_experts` 卸到独立 StorageUnit。双机 Qwen3.6 实验里，master 轨迹数据面峰值从 757 GiB 降到 247 GiB。

动机和本仓库栈选型相同：**原始大对象不要进 Postgres，也不要长期占编排进程内存**。差别是对象类型：

- Dressage：训练 tensor（logprob、expert id），按 token 线性胀，MoE 再乘层数和 top-k
- 本仓库：环境本体（memfile / rootfs）进对象存储；span 事件原文进 S3，检索进 ClickHouse；Postgres 只留 `state_id` 和 URI

不要把 Dressage 的 TransferQueue 直接当快照仓库。可以抄「热路径只留句柄、大字节另放」这一句。

生命周期清理值得看：`dressage/paddock/lifecycle.py` 里，调用方取消了仍在 `create` 的请求，提供方仍会把已创建的租约 `terminate`；`terminate` 超时也不丢任务，转到后台。本仓库的 `SandboxGone`、部分成功的 `Branch` 207，面对的是同一类泄漏。

---

## 7. 对本仓库的结论

**抄：**

1. paddock / sandbox 正交。Firecracker 或 CubeSandbox 只实现放置；agent 怎么调工具放在另一层。
2. 提供方接口保持无知：create、命令、文件、端口。不要把 `register_agent` 写进 VMM。
3. 工厂启动时校验组合，配错立刻失败。
4. 取消和超时后的租约回收，写成硬契约，不要靠调用方记得 finally。
5. 失败可诊断：归档、错误码、不要把基础设施失败当成普通负样本（Harbor RFC 里把 reward=0 和 verifier 挂掉分开）。
6. 大对象下沉。state 行只存 URI。

**不抄：**

1. bubblewrap 槽位重置，或 E2B create/kill，来当 `RestoreState`。那恢复不了当时的进程。
2. 把 paddock 的 pause/resume 当成冻虚机。
3. 用 token segment 代替 action span。训练切开和环境步是两套时钟。
4. 第一期接 slime / Megatron / SGLang / TITO。那是训练平面，本仓库第一期是环境和轨迹平面。
5. 以为 N 路实验 = 开 N 台新沙箱。本仓库要的是同一份 `state` 上的 N 个副本；新沙箱是另一条初始条件。

**Dressage 没有、本仓库必须自己做的：**

- `CommitState`：写出可恢复点（episode 的内部字段，或 firecracker 的四件套）
- `RestoreState`：新 `sandbox_id`，文件和进程都在
- `ReplaySpan(stop_before_t)`：按记录执行 action，observation 必须相等
- `Branch(n)`：同机 CoW 或产品层 clone，不是重新 provision
- `prompt` 属于即将开始的下一轮，模型输出不属于 state

**若以后要接 Dressage 当训练客户：**

本仓库可以当它的一种 `SandboxProvider`，但接口不够。至少要在租约之上加：按 `state_id` 恢复、从当前点 commit、从当前点 branch。Dressage 现有 paddock 的生命周期是 `init → interact → terminate`，一条 trajectory 一份新环境。要做「在错误命令前停住、再采 N 次样」，调用方必须改成 Restore → ReplaySpan → Commit → Branch，而不是再 `create` 一次。

---

## 8. 代码入口

| 路径 | 看什么 |
|---|---|
| `dressage/sandbox/provider.py` | 提供方契约：有 create/terminate，无 snapshot |
| `dressage/sandbox/types.py` | `SandboxSpec` / `SandboxLease` |
| `dressage/sandbox/remote/e2b/provider.py` | E2B 只用 create/kill |
| `dressage/sandbox/local/bwrap/` | 槽位池，terminate = 重置槽 |
| `dressage/paddock/interface.py` | paddock 生命周期；blackbox 的 pause/resume |
| `dressage/paddock/lifecycle.py` | 取消和超时后的回收 |
| `docs/proxy.md` | session / step / TITO / segment |
| `docs/training.md` | segment 展开成训练样本 |
| `docs/staleness.md` | pause 是权重版本，不是环境版本 |
| `docs/sandbox.md` | 提供方文档 |
| `docs/harbor.md` | Harbor 管环境和 verifier，Dressage 管 token |

上游文档入口：https://github.com/Accio-Lab/Dressage/blob/3e3142fe8ea07e4504c3b20a936a4c201a3de44c/README.md
