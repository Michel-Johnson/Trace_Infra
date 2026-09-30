# 官方 Worker 与插件短链

状态：第014轮实现，已随 0d0148e 核心发布上线。Worker使用013的领取、心跳和结果提交核心，计算逻辑在独立子进程执行。注册和计算分别触发；导入、查询、浏览不会运行插件。

当前上线合同为 1.15.0，部署范围见[发布说明](../project/release-20260914.md)；上文轮次与早期合同号保留为实现沿革。

## 包与安装边界

`plugins/worker/registry.json`是随仓库版本发布的允许列表，锁定每个package.json的规范摘要。包声明操作、入口、文件SHA-256及运行时依赖；当前支持Python标准库、自包含单文件入口，不在线下载或安装包。Worker启动和执行前核验源码，随后把核验后的准确字节复制到临时目录执行，避免重新打开一个已变动的源码路径。

OperationVersion中的host/key/package_digest必须与已安装操作的完整定义一致才会被队列领取；仅登记一个陌生key不能让Worker执行任意文件。版本、输入或配置变化产生新请求，已成功的请求不会再执行。

现有两个官方操作：

| 操作 | 输入 | 输出 |
| --- | --- | --- |
| `official.record-counts@1.0.0` | 一个固定轨迹版本，角色source | `trace-hunter.record-counts/1`，角色counts |
| `official.record-report@1.0.0` | 前一步的固定Artifact，角色counts | `trace-hunter.record-report/1`，角色report |

第一个只统计原文spans中的类型、状态和记录数量，并保留采集覆盖声明；第二个计算已记录ok/error结果的错误比例，分母为两者之和，分母为0时比例为null。这是记录统计，不是去重执行、模型成功率或训练资格。两者quality_verdict均为null。

## 启动

使用已锁定环境；DATABASE_URL和TRACE_HUNTER_CONTENT_DIR沿用API的配置，项目需已存在。命令不携带密码。

```bash
.venv/bin/python scripts/invocation_worker.py list
.venv/bin/python scripts/invocation_worker.py register --project my-project
.venv/bin/python scripts/invocation_worker.py run --project my-project
```

`run --once`最多处理一个请求；`run --invocation <id>`只处理指定请求。空队列返回成功，失败/丢失租约的一次执行返回非零。register明确登记允许列表中的操作；run不注册、不导入、不创建Invocation、不自动重试失败任务。

容器共享Backend层，新增命令`invocation-worker`，要求TRACE_HUNTER_WORKER_PROJECT，使用与API相同内容volume。既有worker命令和Compose默认插件服务暂保持原任务通道；可显式启动额外Worker进程。014只修改镜像打包入口，未完成新镜像部署验收。

## 两步串联

1. 查询操作版本与轨迹固定revision/digest，创建record-counts的Invocation。
2. Worker领取执行；客户端按Invocation状态或结果接口读取counts产物引用。
3. 用该准确Artifact引用创建record-report的Invocation，等待Worker执行并读取report产物。

每一步的Artifact保留自身操作ID/版本、完整输入、config与真实提交者；回查第二步的输入可到第一步，再到固定轨迹版本。第一步后补采新轨迹，不改变第二步所用计数。

这是一条显式、实际执行的短链；尚未引入任意DAG或自动分支调度。Base阶段分类接入通用产物在027实现，复用同一边界。

## 进程与故障

插件stdin接收worker-input/1：固定Invocation身份/操作/config，以及输入role/ref、描述和Base64原始内容。读取支持轨迹版本、Artifact及选集清单；选集不会自动递归展开全部成员。所有原文按内容摘要验证后传入，合计原文16MiB、编码信封24MiB。更大的数据需后续远程证据读取通道，不静默截断。

插件stdout返回worker-output/1，包含outputs，每项只含role/content/metadata；content使用规范Base64。stdout信封限16MiB，解码产物合计8MiB，类型/数量由固定操作合同再次校验。子进程不直接连接数据库，也不接受平台或模型凭据环境变量。

Worker默认单次最长60秒，按租约的三分之一周期续租。取消、租约丢失或退出信号停止执行进程组；超时、输出过大、协议错误和内容损坏记录稳定错误码。数据库故障不伪造成功或盲目重跑。处理失败后需显式retry，保留旧attempt和回执。

在Linux/macOS上，每次调用建立独立POSIX会话/进程组。成功、异常、取消、输出超限和超时均清理该组，再读取最终stdout；主进程成功不允许遗留后台子进程。清理不覆盖主动脱离该组的进程，仍需环境层的沙箱/资源限制。

子进程隔离不是安全沙箱；此宿主只运行仓库发布的可信代码。外部Agent与任意代码由后续远程Runtime承载，仍通过固定资源与结果协议连接。
