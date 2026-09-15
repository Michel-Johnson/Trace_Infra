# Trace infra 底层栈：开源对照

只谈语言、数据库、环境怎么存。Fork 切分规则另议。

成熟项目把数据拆成三类，不塞进同一张表：

1. 元数据：谁拥有这次 run、某个 `state_id` 指向哪份快照、父子分叉。放 Postgres。
2. 轨迹事件：span、每一步 action、observation。原始字节先进对象存储；要检索和分析再进 ClickHouse。
3. 环境本体：磁盘和内存。不进数据库，进对象存储里的分层文件。Postgres 只存 URI。

## 和环境 fork 最接近

**E2B infra**（Go）  
https://github.com/e2b-dev/infra  
https://github.com/e2b-dev/infra/blob/main/docs/ARCHITECTURE.md  

控制面 API 用 Go。节点上的 orchestrator 管 Firecracker。Postgres 存 template / snapshot 元数据。Redis 存正在跑的 sandbox 和路由。ClickHouse 存 metrics / events。对象存储（GCS/S3）存 `memfile`、`rootfs.ext4`、`snapfile`、`metadata.json`。创建 sandbox 等于恢复快照；pause 只上传相对模板的脏内存页和磁盘 diff。这是「相同环境」怎么落地的完整样板。

**AgentENV**（Rust 运行时，E2B 兼容 HTTP API）  
https://github.com/kvcache-ai/AgentENV  

Firecracker + overlaybd/ublk 分层根盘。只读层共享，每个 sandbox 写自己的 upper。快照仓库后端是 posix_fs 或 S3 兼容 OSS。支持 snapshot / resume / 同节点 fork。环境存储的关键词是「分层块设备 + 增量快照」，不是整盘复制进数据库。

**forkd**（Rust daemon + Python/TS SDK）  
https://github.com/deeplethe/forkd  

子 VM 用 `mmap MAP_PRIVATE` 共享父 VM 内存，内核做页级 copy-on-write。适合同机大量 fork。持久化仍要落到磁盘/对象存储，不能只靠内存映射。

**Firecracker 本身**（Rust）  
https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md  

快照文件是：VM 状态文件 + guest 内存文件；磁盘是旁路的 backing file。上层系统（E2B/AgentENV）负责打包、diff、上传、恢复。

**containerd overlaybd**（C++/Go）  
https://github.com/containerd/overlaybd  

块级远程镜像，可 live snapshot。AgentENV 用它当根盘层。若环境很大、要按层缓存，看这个，而不是每次 tar 整个容器。

## 和轨迹采集最接近

**Langfuse**（TypeScript）  
https://langfuse.com/handbook/product-engineering/architecture  

Postgres：用户、项目、API key。ClickHouse：traces / observations / scores。Redis：队列。S3：原始 ingestion 事件和附件。他们从「轨迹全放 Postgres」迁走，因为百万行写入和检索都会堵。原始事件先写 S3 再异步进 ClickHouse，S3 相当于 WAL，可重放导入。

**LangGraph checkpointer**（Python）  
官方生产后端是 Postgres（`checkpoints` + `checkpoint_blobs`）。SQLite 只适合单机。大对象应放 S3，库里只留 key。这是 state 元数据怎么建表，不是环境怎么存。

**OpenHands**（Python）  
事件走 `FileStore`：local / S3 / GCS。对话 state 一份 JSON，事件按文件追加。环境（Docker sandbox）和事件存储分开，replay action 不等于 restore 容器。

**Inspect AI**（Python）  
eval log 在本地或 S3。sandbox 默认是 Docker Compose，样本结束后容器会清掉。要保留环境必须自己挂卷或另做快照。checkpoint 主要服务崩溃续跑。

**Temporal**（Go）  
事件历史 + mutable state。Postgres 可起步，大规模写用 Cassandra。history 按 tree/branch 存，支持从某节点开新分支。表内存的是事件 blob，不是 VM 内存。分叉语义可抄，环境文件不能放进这套库。

## 语言怎么选

沙箱编排、快照、热路径：Go（E2B）或 Rust（AgentENV、Firecracker、forkd）。Python 不适合当 Firecracker 控制循环。

轨迹服务、实验脚本、agent 接入：Python 或 TypeScript（Inspect、LangGraph、Langfuse、OpenHands SDK）。

常见拆法：Go/Rust 做环境平面（create/pause/snapshot/fork），Python 做采集和实验 API。两边用 `state_id` 和快照 URI 相连。

## 环境文件具体长什么样

E2B 一份快照在对象存储里大约是：

- `snapfile`：Firecracker VM 状态
- `memfile`：内存（相对模板的 dirty pages）
- `rootfs`：磁盘（copy-on-write overlay / ext4 diff）
- `metadata.json`：CPU、内存、指向哪份模板
- `.header`：块映射，用来按需读取

Postgres 一行 state 只需要：`state_id`、`parent_state_id`、`snapshot_uri`、`template_id`、创建时间。恢复时按 URI 拉 diff 链，不要把内存 dump 塞进 BYTEA。

example 任务没有容器：state 可以是 `task_id + seed + 已重放的 action 前缀`，不必上 Firecracker。真实 shell/GPU 环境才需要上面这套文件。

## 建议对标顺序

1. 环境平面抄 E2B ARCHITECTURE：Go + Postgres + Redis + S3 + Firecracker diff。
2. 轨迹平面抄 Langfuse：事件先 S3，分析进 ClickHouse，Postgres 只管元数据。
3. 分叉历史抄 Temporal 的 history tree（`tree_id` / `branch_id`），或 LangGraph 的 checkpoint 父子。
4. 分层根盘需要时再上 overlaybd / AgentENV，不要第一期自己写块设备。

不要抄的：把 span 和环境 dump 都放进 Postgres；只用 Docker commit 当快照（慢，且不含内存里的进程）；以为 jsonl 重放能替代真实环境快照。
