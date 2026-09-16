# Trace Infra

记录一次 sandbox 运行，在某个检查点冻住，再在同一套环境里解冻。你要的 state 是虚拟机冻住再恢复：当时的文件还在，当时正在跑的进程还在。

把旧命令再执行一遍（`ReplaySpan`）是另一件事，不算这份 state。

仓库只要 Python 3.9+ 标准库就能跑不启动虚拟机的测试。活的 Firecracker 回放必须在本机有可用的 `/dev/kvm`。

---

## 配环境

不测虚拟机时，克隆后装 pytest 即可：

```
git clone https://github.com/Michel-Johnson/Trace_Infra.git
cd Trace_Infra
python3 -m pip install pytest
python3 -m pytest tests/ -q
```

测虚拟机回放时，当前用户还要能读写 `/dev/kvm`。再准备三个文件，默认放在 `/tmp/trace-fc-assets/`（可用环境变量 `TRACE_FC_ASSETS` 改路径）：

- `firecracker`：Firecracker 可执行文件，已知能用 v1.16.1
- `vmlinux`：给 Firecracker 的内核
- `rootfs.ext4`：根盘。里面要有 `/guest_agent`，以及内容为 `exec /guest_agent` 的 `/init`

本机还没有这些文件时，需要 `gcc`、`mkfs.ext4`、能 `mount -o loop`（通常要 sudo）：

```
python3 -m ew_examples.vm_replay --prepare --assets /tmp/trace-fc-assets
```

先看缺什么：

```
python3 -m ew_examples.vm_replay --diagnose --assets /tmp/trace-fc-assets
```

`ready` 为 `true` 才继续跑活测试。更细的步骤见 `docs/spec/vm-replay-test.md`。

---

## 怎么测

不启动虚拟机（这边云主机也是这条）：

```
python3 -m pytest tests/ -q
```

测这台机器能不能做虚拟机回放：

```
python3 -m ew_examples.vm_replay --assets /tmp/trace-fc-assets
```

通过标准：启动虚拟机，写入 `/tmp/marker`（内容 `keepme`），后台启动 `sleep`，`CommitState`，再 `RestoreState` 得到**新的** `sandbox_id`。新虚拟机里文件还在、`sleep` 还在。

---

## 输出格式

`python3 -m ew_examples.vm_replay` 往标准输出打一段 JSON。退出码：`0` 通过，`2` 这台机器测不了，`1` 失败。

`--diagnose` 的字段：

```json
{
  "kvm": true,
  "kvm_path": "/dev/kvm",
  "assets_dir": "/tmp/trace-fc-assets",
  "files": {
    "firecracker": true,
    "kernel": true,
    "rootfs": true,
    "guest_agent": true
  },
  "paths": {
    "firecracker": "/tmp/trace-fc-assets/firecracker",
    "kernel": "/tmp/trace-fc-assets/vmlinux",
    "rootfs": "/tmp/trace-fc-assets/rootfs.ext4",
    "guest_agent": "/tmp/trace-fc-assets/guest_agent"
  },
  "ready": true,
  "notes": []
}
```

`ready` 为 false 时，`notes` 是字符串列表，写出缺 kvm 还是缺哪个文件。

活测试的字段：

```json
{
  "result": "PASS",
  "reason": "RestoreState kept /tmp/marker and the sleep process",
  "diagnose": { "...": "同上一段 diagnose" },
  "parent_sandbox_id": "01...",
  "restored_sandbox_id": "01...",
  "state_id": "01...",
  "use_uffd": true
}
```

`result` 只能是 `PASS`、`FAIL`、`SKIP` 之一。失败时没有那三个 id，`reason` 会写成 `VmmFailed: ...` 或具体哪一步丢了文件/进程。请把整段 JSON 发回来。

---

## 代码怎么用

```
from ew_examples import FirecrackerConfig, Runtime

rt = Runtime("/tmp/trace-run", firecracker=FirecrackerConfig(
    bin="/tmp/trace-fc-assets/firecracker",
    kernel="/tmp/trace-fc-assets/vmlinux",
    rootfs="/tmp/trace-fc-assets/rootfs.ext4",
))
started = rt.start_run("shell", backend="firecracker")
sid = started["sandbox_id"]
rt.act(sid, "exec", {"cmd": "echo keepme > /tmp/marker"})
rt.act(sid, "exec", {"cmd": "sleep 120 &"})
committed = rt.commit_state(sid)
restored = rt.restore_state(committed["state_id"])
rt.act(restored["sandbox_id"], "exec", {"cmd": "cat /tmp/marker"})
```

不传 `backend` 时走进程内 `episode`（内置 `counter` 任务，只给接口测试用）。`restore_and_replay(state_id)` 默认只解冻；传入 `span_id` 才会再跑 `ReplaySpan`。

规格在 `docs/spec/`。
