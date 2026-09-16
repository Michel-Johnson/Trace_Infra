# 第 4 阶段：overlaybd 磁盘分层和 UFFD 内存懒加载

根盘换成 overlaybd 形状：一份只读 base，每个 sandbox 一份 upper。内存恢复改成 userfaultfd 按页加载。`memfile` 只保存相对全零页的 dirty pages。同机 `Branch` 见第 5 阶段。example 任务仍走 `backend=episode`。本仓库保持标准库。

本环境通常没有 `overlaybd-ublk` 和 `/dev/ublk-control`。没有 ublk 时，两台 sandbox 仍然共享同一份 base 的 inode；Firecracker 看到的是按层物化出的私有文件。有 ublk 时走 `LayeredDisk.attach_ublk()`。

活 VM 起不来时（嵌套 KVM 失败），集成测试跳过，不假装启动成功。分层和 UFFD 本身用文件和真实 `userfaultfd(2)` 验收，不依赖微虚拟机。

## 磁盘

```
layers/rootfs/base          共享只读层，硬链接或拷一次模板 rootfs
live/{sandbox_id}/rootfs.ext4   物化后的可写视图，给 Firecracker
snapshots/{state_id}/upper  相对 base 的脏块
snapshots/{state_id}/header 含 disk.blocks 块映射
```

`header.disk`：

```json
{
  "kind": "overlaybd",
  "block_size": 4096,
  "size": 67108864,
  "base_id": "rootfs",
  "base_uri": ".../layers/rootfs/base",
  "upper": "upper",
  "n_dirty": 3,
  "blocks": [
    {"index": 10, "layer": "upper", "offset": 0}
  ]
}
```

未出现在 `blocks` 里的块从 base 读。两台 sandbox 的 `base_inode` 相同；往 A 的 upper 写不影响 B 读到的 base 内容。

## 内存

`PUT /snapshot/load` 的 `mem_backend.backend_type` 是 `Uffd`。handler 先听 jail 里的 `uffd.sock`。Firecracker 把 `GuestRegionUffdMapping[]` 和 userfaultfd 用 SCM_RIGHTS 送来。缺页时 `UFFDIO_COPY` 一次一页。

`memfile` 是打包格式，不是整份 RAM：

```
TDIF | version | page_size | n_pages | n_dirty | 页号... | 页数据...
```

`header.memory`：

```json
{
  "kind": "dirty-pages",
  "page_size": 4096,
  "len": 134217728,
  "n_pages": 32768,
  "n_dirty": 12,
  "dirty": [0, 5, 9]
}
```

没有列入的页按全零恢复。快照体积小于全量 dump。Restore 不必先把整份 `memfile` 读进内存；第一条命令触发的那一页加载即可。

## 验收对照

两台 sandbox 共享只读层（同一 base inode）。`RestoreState` 不必等整份内存读完就能执行第一条命令。快照里的 `upper` 加打包 `memfile` 小于全量 rootfs 加全量 mem dump。
