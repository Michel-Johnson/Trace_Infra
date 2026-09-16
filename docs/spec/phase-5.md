# 第 5 阶段：forkd 同机 BRANCH

`Branch(n)` 对 `backend=firecracker` 从活着的父 sandbox 分出 n 台子机。子机不是把快照全量拷 N 份。内存是一份冻结的 dense memfile，每个 child jail 用硬链接指向同一 inode，Firecracker File backend 按 `mmap MAP_PRIVATE` 映射，内核做页级 copy-on-write。磁盘仍是 overlaybd：同一份只读 base，每个 child 一份 fork 时刻的 upper 拷贝。

example 任务仍走 `backend=episode`（深拷贝 Episode）。没有活 VM 时 firecracker `Branch` 返回 `BranchFailed`。单个 child 启动失败时，该项为 `{"index": i, "error": "BranchFailed", "message": "..."}`，其余 child 仍返回。本仓库保持标准库。

活 VM 起不来时（嵌套 KVM 失败），「两台 child 各自 exec」的集成测试跳过。CoW 本身用 `mmap MAP_PRIVATE` 和硬链接验收，不假装启动成功。

## 文件

```
live/{parent}/fork/{id}/memfile     冻结的一份 guest RAM（dense）
live/{parent}/fork/{id}/snapfile    一份 VM 状态
live/{parent}/fork/{id}/upper       冻结的父盘脏块
live/{child}/memfile                硬链接到上面那份 memfile
live/{child}/upper                  该 child 的私有 upper
live/{child}/rootfs.ext4            物化后的可写盘（无 ublk 时）
```

`parent_state_id` 规则与第 0 阶段相同：父机刚 Commit 且未再 `act` 时填写，否则为 null。

## 验收对照

`Branch(2)` 的两台 child 写内存互不影响；未改过的页来自同一 memfile inode。磁盘未改过的块仍来自同一 base inode。快照 RAM 在磁盘上只有一份，不是 N 份全量 dump。
