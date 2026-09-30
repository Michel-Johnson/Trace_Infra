# 对外 API 与远程 Agent 接入

平台入口是部署地址下的 `/api`，机器合同为 `GET /api/openapi.json`，仓库源为 [contracts/openapi.json](../../contracts/openapi.json)。旧 Case、导入和任务接口使用 `/api/...`；新版本资源、查询、选集和产物使用 `/api/v1/...`。网页、采集脚本和远程 Agent 共用合同；Skill 是可分发指导，MCP 是调用桥，不是另一套业务 API。

新服务能力的实现/验收分别见[迭代记录](../implementation/backend-iterations.md)，后续部署见[本次交付](../project/release-20260914.md)。接入时读取目标服务的 OpenAPI 和 `GET /api/v1/trace-formats`，以该服务返回的 profile 与 Schema 摘要核对输入范围；不能仅用本地草案版本名推断服务器支持的字段。

## 地址、认证和运行边界

Web 的 Caddy 同时提供静态前端与 API 网关。API 和 Plugin 使用独立镜像，复用 Backend 基础层与业务模块；迁移使用 Backend 镜像，PostgreSQL 独立。对外客户端访问 Web 入口，内部 API 和数据库不是额外的公共入口。容器编排见[容器部署](../../deploy/containers/README.md)，现有 systemd 服务见[部署说明](../deployment.md)，职责见[服务边界](../architecture/service-boundaries.md)。

| 身份 | 用途 | 认证位置 |
|---|---|---|
| 操作员 | 浏览、导入、注册插件、创建任务、签发凭据、取消/重试、查看历史证据 | 启用认证的 Caddy 使用 Basic；凭据由部署管理员配置 |
| 项目服务身份 | 新 v1 项目资源的只读查询、显式写入与结果读取 | API 逐请求校验项目、独立 scope、过期与撤销；见[服务身份](service-identities.md) |
| 外部任务执行器 | 领取单个 job；读取固定输入、续租、提交该次结果 | API 校验 job-bound Bearer；领取用 dispatch token，领取后用 lease token + attempt |

新 `/api/v1/*` 的 Bearer 交由 API 检查项目服务凭据；旧任务凭据仅对以下精确路径和方法放行到对应 API 校验，`{family}` 为 `plugin-jobs` 或 `evaluation-jobs`：

- `GET /api/{family}/{job_id}/input`、`record-read`。
- `POST /api/{family}/{job_id}/claim`、`heartbeat`、`query`、`results`、`fail`。

路径后缀分别完整拼接在 `{job_id}/` 后。旧普通 API、`grant/cancel/retry`、job 详情和 `records/{kind}/{record_id}` 不享受该豁免；仅添加 Bearer 头不会绕过 Basic。两种凭据不混在同一次请求中。默认开发配置 `auth-disabled.caddy` 没有 Basic，生产须提供私有认证配置。旧资源仍是单工作空间访问控制；新 v1 的项目身份不会自动改造旧资源的授权。Origin 白名单也不代替认证。

## 新版读取与结果接续

新项目身份可按授权范围查询固定轨迹版本、筛选与聚合，显式创建不可漂移的选集，并读取已有分析产物：

- [轨迹查询](trace-query.md)：字段裁剪、有界分页、未知值及索引缺口。
- [聚合](trace-aggregates.md)：已有事实统计，不调用模型。
- [冻结选集](selections.md)：为后续操作固定成员与摘要。
- [通用产物](artifacts.md)：分类、评分、报告或渲染说明及精确上游引用；实现状态以该页为准。

[操作与计算请求](invocations.md)提供独立的能力发现、固定版本引用与待执行请求。新注册是声明，不自动下载或运行包。

这些读取操作不创建分析、回放或训练消费。旧插件的 facets/evaluation 输出与新 Artifact 是不同资源合同；正式执行绑定在通用 Invocation 接入时完成，不能仅因为 Artifact 声明了 producer 就认为已验证其远程执行。

## 浏览与导入

以下示例在仓库根目录执行。设置可访问的网关地址与操作员用户名；`curl --user` 会提示输入密码，文档和命令不保存密码。自动化可改用部署方提供的私有 `--netrc-file`。

```bash
export TRACE_HUNTER_URL='https://your-trace-hunter.example'
export TRACE_HUNTER_OPERATOR_USER='operator'

curl --fail-with-body --user "$TRACE_HUNTER_OPERATOR_USER" \
  "$TRACE_HUNTER_URL/api/openapi.json"
curl --fail-with-body --user "$TRACE_HUNTER_OPERATOR_USER" \
  "$TRACE_HUNTER_URL/api/collections"
curl --fail-with-body --user "$TRACE_HUNTER_OPERATOR_USER" \
  -H 'Content-Type: application/json' \
  --data-binary @docs/api/examples/run-a.request.json \
  "$TRACE_HUNTER_URL/api/import"
```

导入是每次一个完整 trace 或 catalog JSON 对象，最大 16 MiB；多文件逐份提交，不直接传数组、JSONL 或压缩包。同 ID 同内容可原样重试；同 ID 不同内容返回 409。GET 和导入不会创建计算任务。集合的分页/筛选接口见[集合概览](collection-overview.md)，输入字段见[数据接口](README.md)。

## 显式创建计算与手动领取

原生通用插件使用 `/api/extensions`、`/api/plugin-runs`、`/api/plugin-jobs`；兼容评分插件使用 `/api/plugins`、`/api/evaluations`、`/api/evaluation-jobs`。新接入优先使用原生接口。注册 Manifest 只登记能力；外部实现不会被平台下载、启动或执行，浏览器实现须随 Web 发布。

操作员先注册符合 [Manifest Schema](../../contracts/schemas/plugin-v2.schema.json) 的外部插件，再显式创建运行批次。将下列结构保存为本地请求文件，替换为已注册的插件/贡献点和已导入的 run ID；`config` 须符合该贡献点的配置合同：

```json
{
  "plugin_id": "your.classifier",
  "plugin_version": "1.0.0",
  "contribution_id": "classify",
  "run_ids": ["existing-run"],
  "scope": "task",
  "config": {},
  "collection_id": null,
  "request_key": "first-classification-001"
}
```

单批 1–50 个 run，当前计算范围为 `task` 或 `all`；选择在创建时固定，不随之后的页面筛选变化。相同 `request_key` 和参数复用批次，新计算使用新键。即时 renderer/filter 不创建计算任务。

```bash
# TRACE_HUNTER_CREATE_FILE 指向上面的请求文件。
curl --fail-with-body --user "$TRACE_HUNTER_OPERATOR_USER" \
  -H 'Content-Type: application/json' --data-binary @"$TRACE_HUNTER_CREATE_FILE" \
  "$TRACE_HUNTER_URL/api/plugin-runs"

# 从响应 jobs[].id 选择一个外部 job，设置 TRACE_HUNTER_JOB_ID。
# 领取凭据响应含秘密；写入由操作员指定的私有文件。
umask 077
curl --fail-with-body --user "$TRACE_HUNTER_OPERATOR_USER" -X POST \
  --output "$TRACE_HUNTER_GRANT_FILE" \
  "$TRACE_HUNTER_URL/api/plugin-jobs/$TRACE_HUNTER_JOB_ID/grant"
```

将 `job_id` 和响应中的 `dispatch_token` 通过既有秘密注入方式交给远程 Agent，不需要交付操作员密码。Agent 自行启动后执行：

```bash
# 环境已注入 TRACE_HUNTER_URL、TRACE_HUNTER_JOB_ID、TRACE_HUNTER_DISPATCH_TOKEN。
# TRACE_HUNTER_LEASE_FILE 是本机私有响应文件；此请求不使用 Basic。
umask 077
curl --fail-with-body -H "Authorization: Bearer $TRACE_HUNTER_DISPATCH_TOKEN" \
  -H 'Content-Type: application/json' --data '{"worker":"remote-agent-01"}' \
  --output "$TRACE_HUNTER_LEASE_FILE" \
  "$TRACE_HUNTER_URL/api/plugin-jobs/$TRACE_HUNTER_JOB_ID/claim"
```

响应提供 `attempt`、`lease_token` 和 `lease_seconds`。执行器将前两者置为 `TRACE_HUNTER_ATTEMPT`、`TRACE_HUNTER_LEASE_TOKEN` 后，可读输入或分页查询：

```bash
curl --fail-with-body -H "Authorization: Bearer $TRACE_HUNTER_LEASE_TOKEN" \
  "$TRACE_HUNTER_URL/api/plugin-jobs/$TRACE_HUNTER_JOB_ID/input?attempt=$TRACE_HUNTER_ATTEMPT"
curl --fail-with-body -H "Authorization: Bearer $TRACE_HUNTER_LEASE_TOKEN" \
  -H 'Content-Type: application/json' --data '{"kind":"spans","limit":20}' \
  "$TRACE_HUNTER_URL/api/plugin-jobs/$TRACE_HUNTER_JOB_ID/query?attempt=$TRACE_HUNTER_ATTEMPT"
```

然后按输出 Schema 生成结果，以同一 Bearer 和 `attempt` 调用 `POST …/results`；用 `--data-binary @"$TRACE_HUNTER_RESULT_FILE"` 提交。`input_ref` 从固定输入接口原样引用，结果与输入摘要、插件版本、配置和本次尝试绑定。长任务调用 `POST …/heartbeat?attempt=N` 续租；执行器故障调用 `POST …/fail?attempt=N`，不要把故障当评分为零。dispatch 一次使用、1 小时过期；租约当前为 120 秒，直接 HTTP 执行器至少每 60 秒续租。领取响应丢失时由操作员取消、重试并重新签发，不能猜测 attempt 或复用未知租约。

## Agent 使用 MCP

远端 Agent 也可用现有的 stdio 桥代替手写 HTTP。可以使用[可选 agent-tools 镜像](../../deploy/containers/README.md#客户端工具镜像)，或保留完整仓库和锁定 Python 环境后，在 Agent 所在机器启动：

```bash
export TRACE_HUNTER_JOB_KIND=plugin
# URL、JOB_ID 和 DISPATCH_TOKEN 由启动环境注入。
.venv/bin/python scripts/evaluation_mcp.py
```

把该命令配置为 Agent 的 stdio MCP 子进程；不是运行在 Web 镜像里的公共 MCP 服务。工具顺序为 `plugin_claim → plugin_get → trace_query / record_read → plugin_submit`，也提供 `plugin_heartbeat` 和 `plugin_fail`。桥接进程保管 token，领取后每 40 秒续租；一次进程绑定一个 job。未设置 `JOB_KIND` 时为兼容 evaluation 模式。当前没有 Streamable HTTP MCP、SDK 服务发现或远程 Agent 自动启动接口。

## 共同合同与版本

HTTP 以线上 OpenAPI 与同版本的 `contracts/openapi.json` 为准；前端生成类型、采集适配器和外部执行器都消费同一合同。输入 `schema_version`、插件 Manifest 版本、插件实现版本和结果协议分别管理，不从 URL 推断。输入不兼容变化需要新 Schema/adapter；已注册插件同 ID+版本不可改实现，结果追加保存，原 trace 与 digest 不变。技能及适配器的发布责任见 [skills/README](../../skills/README.md)，完整贡献点和结果示例见[通用插件指南](../guides/general-plugins.md)。
