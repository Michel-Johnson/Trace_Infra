# 容器部署与开发边界

运行图：`PostgreSQL healthy → migrate 成功 → 当前投影回建成功 → API / Plugin Worker → Web`。Web 和外部 Agent 使用同一套 `/api` 与 OpenAPI；网关按 operator Basic Auth 和单任务 Bearer 分开鉴权。API 和 Plugin 使用独立镜像，复用 Backend 基础层；[模块与远程服务边界](../../docs/architecture/service-boundaries.md)说明每部分职责。

| 镜像 | 用途 | 运行依赖 |
|---|---|---|
| `trace-hunter/web:<release>` | React 静态产物 + Caddy 同源网关 | API |
| `trace-hunter/api:<release>` | 对外 HTTP 接口，调用 Backend 业务模块 | Backend 构建层、PostgreSQL；等待迁移完成 |
| `trace-hunter/backend:<release>` | 共享业务模块、锁定依赖；一次性 `migrate` 与 `rebuild-indexes` | 迁移和投影回建时依赖 PostgreSQL |
| `trace-hunter/plugin:<release>` | 官方插件 Worker | Backend 构建层、PostgreSQL；等待迁移完成 |
| 官方 PostgreSQL 18.6 | 持久数据库 | 独立 volume |
| `trace-hunter/agent-tools:<release>` | 可选客户端：导入 CLI、skills、job-bound stdio MCP | 按命令挂载输入或连接平台；不是常驻平台服务 |

Web、API、Plugin 分进程和开发边界。Backend 是共享模块和镜像基础层，当前没有第二套 Backend HTTP 服务。`contracts/` 是前后端及插件共用的合同来源。API / Plugin 同一发布使用同一个 Backend 构建层，各自有独立启动入口。

开发目录对应：`apps/web` 负责页面，`apps/api` 负责 HTTP，`src/trace_hunter` 负责领域逻辑，`plugins` 负责扩展，`skills` 负责 Agent 指导，`contracts` 负责协议，`db/migrations` 负责数据库演进。前端可用 `npm run dev:mock` 独立开发；跨端字段变更先更新合同，再同步实现。当前保留一个仓库管理这些单元。

## 首次本地启动

需要 Docker Engine 与支持 `service_completed_successfully` 的 Docker Compose v2+。在仓库根目录执行。默认只绑定本机，不接触当前远端 systemd 服务或已有数据库。

```bash
python3 scripts/configure_container_env.py \
  --directory var/containers/local --username admin --version local
```

命令会提示 Web 密码，生成数据库随机密码及 Caddy 密码摘要。仅运行临时密码哈希容器，不启动平台。私有目录权限为0700，配置0600；secret文件位于这个私有目录内，并允许容器运行用户读取。不会覆盖已存在的配置。原始密码不写入源码、镜像或命令行参数。

```bash
docker compose --env-file deploy/containers/images.env \
  --env-file var/containers/local/deployment.env \
  -f deploy/containers/compose.yaml build migrate api plugin web

docker compose --env-file deploy/containers/images.env \
  --env-file var/containers/local/deployment.env \
  -f deploy/containers/compose.yaml up -d --no-build --wait
```

访问 `http://127.0.0.1:8766/`。其他端口在私有 `deployment.env` 中设置；改端口时同步修改 `PUBLIC_ORIGIN`，用于浏览器请求的来源检查。`WEB_BIND` 改为实际部署的监听地址后，才开放到相应网络。对外API说明见[external-access](../../docs/api/external-access.md)。

Compose 只发布 Web 端口，PostgreSQL/API 使用内部服务名。迁移失败会阻止 API/Plugin 启动，不以应用重试掩盖迁移错误。停止用相同参数执行 `down`，默认保留 volumes；不要在正式数据上使用 `down -v`。早期容器配置的 `worker` 服务现名为 `plugin`；如已运行旧配置，更新时增加 `up --remove-orphans` 以退出旧 Worker。远端 systemd 的服务名称不变。

## 插件与远程服务

`plugin` 容器运行当前仓库随版本发布的官方计算插件，不开放公共端口。renderer / filter 由 Web 中的浏览器宿主加载；外部计算插件由独立程序或远程 Agent 运行，经平台 API 领取任务、查询输入和提交结果。普通客户端读取集合、Case、轨迹和已有结果，使用操作员认证；单任务执行器使用范围受限的 Bearer。

远端 Agent 使用自身部署、模型和沙箱配置。它可选用下面的 agent-tools 镜像作为 HTTP/MCP 客户端；agent-tools 本身不包含模型服务。远程服务不成为平台启动的硬依赖，暂时不可用不会阻止轨迹浏览。当前尚无远端服务注册、自动拉起或主动推送任务功能；已有的手动任务接入见[对外 API](../../docs/api/external-access.md)。

## 客户端工具镜像

```bash
docker compose --env-file deploy/containers/images.env \
  --env-file var/containers/local/deployment.env \
  -f deploy/containers/compose.yaml --profile tools build tools

docker run --rm trace-hunter/agent-tools:local \
  /app/skills/trace-hunter-adapter/scripts/prepare.py --help
```

导入工具需要读取的包/来源应挂到 `/work`，输出目录使用当前容器用户可写的挂载目录。工具镜像内完整保留 `/app/skills` 到 `/app/scripts` 的关系，不能从中只拷贝一个SKILL.md就声称脚本可独立运行。[技能目录](../../skills/README.md)

远端 Agent 使用 MCP 时，`private-job.env` 由用户以0600保存 `TRACE_HUNTER_URL / TRACE_HUNTER_JOB_ID / TRACE_HUNTER_DISPATCH_TOKEN / TRACE_HUNTER_JOB_KIND=plugin`：

```bash
docker run --rm -i --env-file /private/private-job.env \
  trace-hunter/agent-tools:local /app/scripts/evaluation_mcp.py
```

这是客户端进程启动的 stdio MCP。平台仍不自动启动远端 Agent，也没有在本轮新增 Streamable HTTP MCP 或SDK产品。

## 镜像和依赖版本

- `images.env` 引用 Docker Official Images 的精确版本与多平台 index digest；`images.lock.json` 保存官方标签元数据、发布/推送时间、amd64/arm64摘要。
- 当前基础镜像均早于仓库截止 `2026-09-03T00:00:00Z`。后续升级必须重新核验来源、时间和digest，更新两份文件；不要改成 latest。
- Python 只安装现有 `apps/api/requirements.lock` 的二进制包并校验hash；前端使用 `npm ci --ignore-scripts` 和现有lock。不在构建中升级pip、安装新系统包或重新解析宽版本范围。
- 应用镜像用提交/发布版本标记。生产发布时用镜像仓库的不可变digest部署；本轮只定义并本地构建，没有推送到未指定的镜像仓库。
- API启动会校验已发布插件文件。后端镜像特意包含package.json引用的少量Web源码，防止插件包校验失效；未来包结构重整再解除此构建期关联。

## 持久数据与现有服务迁移

PostgreSQL 18 使用 `/var/lib/postgresql` 挂载点，Compose 数据与镜像分离。此栈不会自动读取、导入或覆盖远端 `trace-hunter-data`。

现有最早几条 legacy trace 的额外轮次/顺序投影依赖 `apps/trace-lab` 私有原件。切换既有生产服务时，需保留来源并只读挂载到 `/app/apps/trace-lab`（API 与 Plugin Worker 都需要），或先把这些来源转换成自包含文档；不能把私有原件打入镜像。修复后的50条标准样本不依赖这些特殊文件名。

正式切换应先备份数据库与全部来源，在隔离数据库恢复并核对运行/产物摘要、编号与分类结果；验证镜像后再切入口。回退应用镜像不回滚已有数据库数据。本轮没有执行这次生产切换。

## 本地验证记录

2026-09-11，Docker Desktop、Linux arm64：

- Web、Backend、agent-tools 三个镜像构建成功，前端 TypeScript 与生产构建通过。
- 新建隔离 PostgreSQL 数据卷，迁移成功后 API、Worker、Web 启动；入口和 API 认证通过。
- 合成轨迹导入、重复导入、原始 digest 保持不变；导入不创建分析任务，显式 Base 分类完成。
- agent-tools 通过网关以单任务 Bearer 完成 MCP 初始化、领取、查询、读取记录、续租和分类结果提交。
- 后端入口的 9 项定向测试通过，技能包装脚本与转换 CLI 可启动。
- 合入消息级 CSV 适配器后重新构建三个镜像；工具容器内 4 项 CSV 转换/打包测试通过，重启后已有输入摘要保持一致，Base 分类再次完成。
- API / Plugin 拆分后，Web、API、Backend、Plugin、agent-tools 五个应用镜像构建通过；新建隔离数据卷完成启动、HTTP 读取、幂等导入、显式分类和鉴权验证，导入不触发计算、输入摘要不变。

amd64 基础镜像摘要已锁定，应用镜像尚未在 amd64 上构建验证。未迁移生产数据库、未切换线上服务、未发布镜像到远端仓库。

启动依赖采用 Compose 的健康条件与一次性成功条件：[Docker 官方说明](https://docs.docker.com/compose/how-tos/startup-order/)。
