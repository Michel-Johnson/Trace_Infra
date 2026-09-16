# 第 0 阶段：契约

本文件是已确认开发计划的第 0 阶段产出。覆盖四条接口、Postgres 表、S3 key、span 事件。第 1 阶段在本仓库 `Episode` 上实现语义；Firecracker 从第 3 阶段才出现。example 任务的 `brief`、`actions()`、`score()` 不改。

后端分两种，接口相同：`episode`（seed 加重放）和 `firecracker`（微虚拟机）。调用方只使用下面的字段，不直接依赖 VMM。

标识符一律用 ULID 字符串。时间用 UTC 秒，小数两位，和现有 `trajectory.jsonl` 的 `ts` 一致。

---

## 1. 接口

四条写接口，一条只读。`sandbox_id` 是活句柄，进程重启后作废；持久点只有 `state_id`。

### CommitState

把当前 sandbox 写成一个可恢复检查点。

请求：

```json
{
  "sandbox_id": "01J...",
  "prompt": {"system": "...", "transcript": [{"role": "user", "content": "..."}]}
}
```

`prompt` 是这一拍要送给模型的完整输入。它属于即将开始的下一轮模型调用，不是上一轮的模型输出。省略 `prompt` 时，该 state 仍可恢复环境，但不能做「同一输入下 N 次采样」。

返回：

```json
{
  "state_id": "01J...",
  "run_id": "01J...",
  "t": 7,
  "parent_state_id": "01J...",
  "snapshot_uri": "s3://bucket/snapshots/01J.../",
  "prompt_uri": "s3://bucket/runs/01J.../prompts/01J....json"
}
```

`t` 是检查点时刻已经完成的 action 序号，对应 `Episode.t`。`parent_state_id` 为 null 表示 run 的起始 state。

### RestoreState

请求：`{"state_id": "01J..."}`

返回：

```json
{
  "sandbox_id": "01J...",
  "state_id": "01J...",
  "run_id": "01J...",
  "t": 7,
  "backend": "episode",
  "prompt": {"system": "...", "transcript": []}
}
```

`prompt` 从 `prompt_uri` 读出；该 state 写入时没有 prompt 则为 null。Restore 不执行 span，不调模型。

`episode` 后端：`load_task(task_id, seed)`，恢复 budget、`t`、`done`、`result` 和 Task 以下划线开头的内部字段。这些内部字段不得出现在任何 observation 里，只给 Restore 使用。

`firecracker` 后端：按该 state 的 snapfile、memfile、rootfs、header 恢复。第 3 阶段实现。

### ReplaySpan

从当前 sandbox 所在 state 起，按记录执行 action，停在指定步。

请求：

```json
{
  "sandbox_id": "01J...",
  "span_id": "01J...",
  "stop_before_t": 12
}
```

`stop_before_t` 必填。执行 span 中所有 `t < stop_before_t` 的事件。错误命令若发生在 `t=12`，则 `stop_before_t=12`，环境停在错误前。

返回：

```json
{
  "sandbox_id": "01J...",
  "span_id": "01J...",
  "from_t": 8,
  "stopped_before_t": 12,
  "last_t": 11,
  "events_applied": 4
}
```

重放时用记录里的 `action` 和 `params` 调用环境，忽略记录里的模型输出。每一步的完整 observation 必须与记录相等，否则返回错误 `ReplayDivergence`，附上 `t`、期望、实际。不相等就不能做 N 路对照。

ReplaySpan 不调模型，不写新的 state。

### Branch

请求：`{"sandbox_id": "01J...", "n": 3}`

`n` 为整数，范围 1 到 100。

返回：

```json
{
  "parent_sandbox_id": "01J...",
  "parent_state_id": null,
  "children": [
    {"sandbox_id": "01J...", "index": 0},
    {"sandbox_id": "01J...", "index": 1},
    {"sandbox_id": "01J...", "index": 2}
  ]
}
```

`parent_state_id` 仅在调用前刚 CommitState 且尚未 ReplaySpan 时填写；若刚 ReplaySpan 过，当前点还没有 state_id，则为 null。实验要挂到某个检查点上时，必须先 CommitState 再 Branch，或 Branch 之后立刻对每个 child CommitState。推荐顺序：RestoreState → ReplaySpan → CommitState（错误前检查点）→ Branch。

`episode` 后端：深拷贝 N 份 Episode 和 Task，互不共享可变对象。`firecracker` 后端：forkd BRANCH。部分成功时仍返回 `children` 数组，失败项为 `{"index": 1, "error": "BranchFailed", "message": "..."}`，HTTP 状态 207。

### GetSpan（只读）

请求：`{"span_id": "01J..."}`

返回 span 元数据和按 `t` 升序的事件列表。事件含完整 `observation`，不是截断后的 `obs_summary`。

### 错误

| code | 何时 |
|---|---|
| `StateNotFound` | `state_id` 不存在 |
| `SpanNotFound` | `span_id` 不存在 |
| `SandboxGone` | `sandbox_id` 已结束或进程内不存在 |
| `ReplayDivergence` | 重放 observation 与记录不等 |
| `StopBeforeTOutOfRange` | `stop_before_t` 不在该 span 的 t 范围内 |
| `MissingPrompt` | 要用同一 prompt 采样，但该 state 没有 `prompt_uri` |
| `BranchFailed` | 单个 child 失败 |

实验主路径：RestoreState → ReplaySpan(stop_before_t=错误步) → CommitState（带 prompt）→ Branch(n) → 每份 child 调模型并 `act`。

---

## 2. Postgres

第 2 阶段落地（见 `docs/spec/phase-2.md`）。第 1 阶段曾用同等字段的本地 JSON；第 2 阶段改为 SQL，列名保持一致。

```sql
CREATE TABLE runs (
  run_id        TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL,
  seed          INTEGER NOT NULL,
  backend       TEXT NOT NULL CHECK (backend IN ('episode', 'firecracker')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE states (
  state_id         TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL REFERENCES runs (run_id),
  parent_state_id  TEXT REFERENCES states (state_id),
  from_span_id     TEXT,
  t                INTEGER NOT NULL,
  backend          TEXT NOT NULL CHECK (backend IN ('episode', 'firecracker')),
  snapshot_uri     TEXT NOT NULL,
  prompt_uri       TEXT,
  budget           JSONB NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE spans (
  span_id         TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL REFERENCES runs (run_id),
  from_state_id   TEXT NOT NULL REFERENCES states (state_id),
  to_state_id     TEXT REFERENCES states (state_id),
  sandbox_id      TEXT,
  t_start         INTEGER NOT NULL,
  t_end           INTEGER,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE span_index (
  span_id     TEXT NOT NULL REFERENCES spans (span_id),
  t           INTEGER NOT NULL,
  event_uri   TEXT NOT NULL,
  action      TEXT NOT NULL,
  status      TEXT NOT NULL,
  PRIMARY KEY (span_id, t)
);
```

`states.budget` 对应 `Budget.snapshot()`。`span_index` 只存检索字段；完整事件在 S3。ClickHouse 第 6 阶段再加，不进第 0 到 5 的验收。

活着的 sandbox 只存在编排进程内存里，不进 Postgres。

---

## 3. S3

桶内前缀固定。`{id}` 都是 ULID。

```
snapshots/{state_id}/metadata.json
snapshots/{state_id}/snapfile
snapshots/{state_id}/memfile
snapshots/{state_id}/rootfs
snapshots/{state_id}/header
snapshots/{state_id}/episode.json

runs/{run_id}/prompts/{state_id}.json
runs/{run_id}/spans/{span_id}/events/{t:08d}.json
```

`episode.json` 仅 `backend=episode`：可恢复的 Task 内部状态、`t`、`done`、`result`。不得当作 observation 返回给 solver。

`snapfile` / `memfile` / `rootfs` / `header` 仅 `backend=firecracker`，含义与 E2B 快照文件相同。第 3 阶段才写这些对象。第 1 阶段 `snapshot_uri` 仍指向 `snapshots/{state_id}/`，目录里只有 `episode.json` 和 `metadata.json`。

`metadata.json`：

```json
{
  "state_id": "01J...",
  "run_id": "01J...",
  "backend": "episode",
  "task_id": "clinical_signal",
  "seed": 3,
  "t": 7,
  "protocol": 1
}
```

`prompts/{state_id}.json` 即 CommitState 传入的 `prompt` 原文。

事件对象 key 中的 `{t:08d}` 与事件内字段 `t` 相同，便于按错误步直接取文件。

---

## 4. Span 事件

一行一次环境调用，对齐现有 `trajectory.jsonl`，并补上重放所需的完整 observation。

现有 jsonl 字段保留：`t`、`ts`、`action`、`params`、`status`、`cost`、`budget_remaining`、`obs_summary`、`error`。

新增必填：

| 字段 | 类型 | 说明 |
|---|---|---|
| `protocol` | int | 与回复信封相同，现为 1 |
| `run_id` | string | |
| `span_id` | string | |
| `sandbox_id` | string | 当时的活句柄，仅审计 |
| `observation` | object 或 null | 完整 observation；`status=error` 且无 observation 时为 null |

`status=error` 时保留现有的 `error`，并可有 `message`。`cost` 对应信封里的 `cost_charged`。

第 1 阶段写本地 jsonl 时：每一行同时含 `observation` 和 `obs_summary`。`obs_summary` 仍用现在的 `_summarise`，给人读；`ReplaySpan` 只使用 `observation`。

一条 span 是两个 state 之间的短轨迹：事件的 `t` 从 `from_state.t+1` 连续增到下一个 CommitState 的 `t`。中间不插空号。`Episode.act` 仍按现有逻辑给 `t` 加一，任务代码不改。

模型输出不属于 span 事件。它在 child 上调用模型之后，才变成下一次 `act` 的 `action` / `params`。prompt 挂在 state 上。

---

## 第 1 阶段验收对照

五个 example 任务、同一 `task_id+seed`：CommitState 后 RestoreState，再 ReplaySpan 到某 `stop_before_t`，环境的 budget、`t`、后续 `act` 的 observation 必须与一次连续跑到该步相同。Branch(n=2) 之后，两份 child 各自 `act` 互不影响。任务文件不改。
