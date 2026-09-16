# 第 3 阶段：单机虚机最小切片

Firecracker 从模板 rootfs 启动 sandbox。`CommitState` 写出 `snapfile`、`memfile`、`rootfs`、`header`。`RestoreState` 按这些文件恢复并继续 `exec`。磁盘是普通 ext4，不是 overlaybd。UFFD 和 forkd 不在本阶段。example 任务仍走 `backend=episode`。

## 怎么调用

```
Runtime(root, firecracker=FirecrackerConfig(bin=..., kernel=..., rootfs=...))
rt.start_run("shell", backend="firecracker")
rt.act(sandbox_id, "exec", {"cmd": "echo hi"})
rt.commit_state(sandbox_id)
rt.restore_state(state_id)
```

`Branch` 对 firecracker 返回 `BranchFailed`：那是第 5 阶段的 forkd。

## 快照文件

```
snapshots/{state_id}/metadata.json
snapshots/{state_id}/snapfile
snapshots/{state_id}/memfile
snapshots/{state_id}/rootfs
snapshots/{state_id}/header
```

`header` 是 JSON：vcpu、内存、vsock 端口。overlaybd 的块映射和第 4 阶段的 dirty-page `memfile` 见 `docs/spec/phase-4.md`。

Guest 里的 `guest_agent` 听 vsock 5252。一条 span 事件的 action 是 `exec`，observation 是 `{exit, stdout, stderr}`。

活 VM 的 jail 在 `{root}/live/{sandbox_id}/`，磁盘文件名是相对路径 `rootfs.ext4`，这样 Restore 换目录时 snapfile 里的盘路径还能对上。

## 验收对照

恢复后，当时在跑的进程还在，当时写过的文件还在。没有工作的 KVM（包括嵌套虚拟化失败）时，集成测试跳过，不假装启动成功。
