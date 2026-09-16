# 怎么测：这台机器能不能做虚拟机回放

这里的「回放」指的是 `RestoreState`：把当时的内存和磁盘冻住，再在一台新的 sandbox 上解冻。解冻之后，当时写过的文件还在，当时正在跑的进程还在。

`ReplaySpan` 是另一件事。它会把记录下来的 `exec` 再执行一遍。那不是你要的 state。作业题那条 `episode` 路径也不是。本页只测 Firecracker 这条。

这份代码可以在没有 KVM 的机器上写完。活测试必须在本机跑：需要能用的 `/dev/kvm`，以及 `firecracker`、内核、带 guest agent 的 rootfs。

---

## 通过长什么样

1. 虚拟机启动成功。
2. 往 guest 里写 `/tmp/marker`，内容是 `keepme`。
3. 在 guest 里后台启动 `sleep`。
4. `CommitState` 写出 `snapfile`、`memfile`、`header`。
5. `RestoreState` 得到一个**新的** `sandbox_id`。
6. 新 sandbox 里 `cat /tmp/marker` 仍是 `keepme`。
7. 新 sandbox 里 `pgrep sleep` 仍能找到那个进程。

六、七两条同时成立，才算这台机器能做虚拟机回放。只把命令重跑一遍不算。

---

## 本机要准备什么

`/dev/kvm` 当前用户要能读写。普通笔记本打开虚拟化即可。云主机如果是套娃虚拟化，经常会在 `InstanceStart` 时失败，那种机器测不了。

准备三个文件，默认放在 `/tmp/trace-fc-assets/`，也可以改环境变量 `TRACE_FC_ASSETS`：

- `firecracker`：Firecracker 可执行文件。已知能用的是 v1.16.1。
- `vmlinux`：给 Firecracker 的内核。
- `rootfs.ext4`：ext4 根盘。里面必须有 `/guest_agent`，以及 `/init` 去执行它。`/init` 的内容就是：

```
#!/bin/sh
exec /guest_agent
```

guest agent 的源码在仓库里：`ew_examples/guest_agent.c`。编译：

```
gcc -static -O2 -o /tmp/trace-fc-assets/guest_agent ew_examples/guest_agent.c
```

如果本机还没有这三个文件，可以让脚本去拉并做根盘（需要 `gcc`、`mkfs.ext4`、能 `mount -o loop`，通常要 sudo）：

```
python3 -m ew_examples.vm_replay --prepare --assets /tmp/trace-fc-assets
```

`--prepare` 会下载 Firecracker v1.16.1、firecracker-ci 的 `vmlinux-6.1.186`、Alpine minirootfs，再做成 64MiB 的 `rootfs.ext4`。下载地址如果 404，把三个文件自己放到 `--assets` 目录即可，不必改代码。

先看这台机器缺什么：

```
python3 -m ew_examples.vm_replay --diagnose --assets /tmp/trace-fc-assets
```

`ready` 为 true 才值得往下跑活测试。`ready` 为 false 时，`notes` 会写出缺 kvm 还是缺文件。

---

## 跑检查

在仓库根目录：

```
python3 -m ew_examples.vm_replay --assets /tmp/trace-fc-assets
```

退出码：`0` 是 PASS，`2` 是 SKIP（没有 kvm 或没有资产，这台机器测不了），`1` 是 FAIL（能启动但恢复后文件或进程丢了，或者 Firecracker 启动失败）。

标准输出是一段 JSON，字段如下。

`--diagnose`：

| 字段 | 含义 |
|---|---|
| `kvm` | `/dev/kvm` 当前用户能否读写 |
| `kvm_path` | 设备路径，没有则为 null |
| `assets_dir` | 资产目录 |
| `files` | `firecracker` / `kernel` / `rootfs` / `guest_agent` 是否存在 |
| `paths` | 上面四个文件的绝对路径 |
| `ready` | kvm 加上 firecracker、内核、rootfs 都齐了才是 true |
| `notes` | 字符串列表。`ready` 为 false 时写出缺什么 |

活测试（默认命令）：

| 字段 | 含义 |
|---|---|
| `result` | `PASS` / `FAIL` / `SKIP` |
| `reason` | 一句话。PASS 固定为 `RestoreState kept /tmp/marker and the sleep process` |
| `diagnose` | 同上一段 diagnose 对象 |
| `parent_sandbox_id` | 冻住之前的 sandbox。仅 PASS |
| `restored_sandbox_id` | 解冻后的新 sandbox。仅 PASS |
| `state_id` | 这次 CommitState 的 id。仅 PASS |
| `use_uffd` | 这次恢复是否先走 UFFD。仅 PASS |

FAIL 时没有那三个 id。`reason` 会带 `VmmFailed:` 或指出 `/tmp/marker`、`sleep` 哪一步没了。把整段 JSON 发回来即可。

默认恢复走 UFFD。若 UFFD 在你机器上失败，代码会把打包的 `memfile` 解成整份内存，再改用 Firecracker 的 File backend 试一次。只想走 File 时：

```
python3 -m ew_examples.vm_replay --assets /tmp/trace-fc-assets --file-backend
```

同一件事也可以当 pytest 跑。没有 kvm 或 `InstanceStart` 失败时会 skip，不会假装成功：

```
python3 -m pytest tests/test_trace_vm_replay.py -q
```

---

## 用 Runtime 自己写

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

`restore_and_replay(state_id)` 只解冻。只有你显式传入 `span_id` 时，它才会再跑 `ReplaySpan`。测 state 时不要传 `span_id`。

`start_run` 的 `task_id` 在 firecracker 后端只是记录用，不会去跑作业题。作业题仍然走 `backend=episode`。

---

## 失败时看哪里

`InstanceStart failed or hung`：这台机器的 KVM 不能启动微虚拟机。改本机虚拟化设置，或换一台有真正 `/dev/kvm` 的机器。不要在套娃虚拟化失败的机器上假装测过。

`guest agent not ready`：rootfs 里没有 `/init` → `/guest_agent`，或 vsock 没起来。先用 `--diagnose`，再确认 `/init` 和静态编译的 `guest_agent`。

`/tmp/marker missing`：磁盘层没恢复。看快照目录里有没有 `upper` 和 `header`。

`sleep process gone`：内存没恢复。看 `memfile` 是不是空的，以及恢复日志里 Uffd / File 哪一条成功了。

把上面那段 JSON 和 `live/{sandbox_id}/fc.log` 发回来，就可以继续改。
