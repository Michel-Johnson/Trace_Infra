# 依赖发布冷却期

按用户要求，本次发布只使用 **2026-09-17 00:00:00 UTC 及以前**发布的包；该截止时间距 2026-09-24 发布至少七天，覆盖直接、传递和可选依赖。容器镜像仍使用原来更严格的 2026-09-03 截止锁，不升级镜像。7 天冷却期之外仍须校验锁文件、来源与哈希。

## 可复现安装

- npm：直接依赖固定版本，提交 `package-lock.json`，`.npmrc` 固定 `before` 并设置 `ignore-scripts=true`。部署仅用 `npm ci --ignore-scripts`。锁定所有平台可选包的 registry 来源和 SHA-512。
- Python：提交三个含哈希的锁文件，`uv.toml` 设置 `exclude-newer` 和 `no-build`。安装使用 `--require-hashes --only-binary :all:`，避免执行第三方源码构建。API 锁是完整服务环境，根锁与采集锁分别用于轻量工具和采集器。
- `scripts/verify_dependencies.py` 对照 npm / PyPI 官方元数据核验版本日期、源地址与哈希；PyPI 按每个允许安装的构件上传时间检查，避免旧版本后来增加新构件。报告保存到 `dependency-verification.json`。

当前报告覆盖 **229 个 npm 版本、30 个 Python 版本**（三个锁文件去重，另含 Claude CLI 和八个平台可选包）。新增 `grpcio 1.83.1`、`opentelemetry-proto 1.44.0` 与 `protobuf 7.36.1` 均使用官方 PyPI 构件。浏览器宿主与 Recorder 的 npm 构件均逐一核对官方发布日期、tarball 地址与 SHA-512；未放宽旧版锁文件。

2026-09-10 的前端 `npm audit` 为 0 个已知漏洞，本次未重新执行漏洞审计。`js-yaml` 固定 4.3.2 修复上游依赖链的已知告警，该版本发布于 2026-08-26。

远端内部 Python 镜像不返回上传时间，因此不使用它绕过日期门禁。本次先在本机核验官方日期与所有哈希，下载同一锁文件允许的 Linux wheels 后上传，再执行：

```bash
uv --no-config pip sync --python .venv/bin/python --require-hashes --no-build --no-index --find-links var/wheels apps/api/requirements.lock
```

这里 `--no-config` 只用于没有发布时间元数据的**已核验离线目录**；不准用于联网重新解析版本。安装仍禁止构建并要求锁定哈希。

## 服务程序

内置 Agent 的 Claude Code CLI 是独立服务器程序，不加入前端 npm 锁。Recorder 固定 `@anthropic-ai/claude-code@2.1.274`（官方 npm 发布时间 `2026-09-16T22:36:09Z`，SHA-512 SRI `sha512-UI8TGoOO0fYT38VSoAjtu9C0EQkOwgwA4+ETFCgQhTO9NZvpgCERANL0UzioEW89b2zbcoL1B3z+7W37QdBxOA==`）及同版本 `linux-x64` 原生包（`2026-09-16T22:46:37Z`，SRI `sha512-xiC1514UgW1aRvHOtfgUVUACWtOwm42TIeQlllHFAMFLrBYy+cG+MfSTlTYnjUPpu4zmUeEDEs2VdZL/iLIwGQ==`）。所有平台可选包的精确 SRI 在 `scripts/verify_dependencies.py` 中核验，均早于截止线。服务器使用私有目录、`npm install --ignore-scripts --before=2026-09-17T00:00:00Z --save-exact` 安装；worker 启动时要求 `claude --version` 为 `2.1.274`。模型令牌仅在私有环境中配置。

| 程序 | 版本 / 发布时间 | 来源与 SHA-256 |
|---|---|---|
| PostgreSQL | 18.6 / 2026-08 | 官方 `ftp.postgresql.org/pub/source/v18.6/postgresql-18.6.tar.bz2`；`555610c24d53e4316da5b7d3fc25c279d96856d5e0e23ee308c328c5fa881d9f` |
| Caddy | 2.11.4 / 2026-06-03 | 官方 `github.com/caddyserver/caddy/releases` 的 Linux amd64 构件；`527fbf917c39189a1e3b31d34fa955601680b2d5c8055d2a87b8b9588dec7bb9` |

本地网关测试使用同版本 [macOS arm64 官方构件](https://github.com/caddyserver/caddy/releases/tag/v2.11.4)，归档 SHA-256：`9efb0af2d6cf09cfb5053c0e51721b9b3d4956d346234f39368d943d25a3c9a7`。同时核对官方 `checksums.txt` 中的 SHA-512；该校验清单使用 SHA-512，不是 SHA-256。只提取已核验的 `caddy` 二进制用于测试，不改变产品依赖。

PostgreSQL 从校验后的官方源代码本地构建；这是明确的基础设施构建，不是允许 pip 任意构建包。构建所需 bison 3.3.2.dfsg-1、flex 2.6.4-6.2 及对应开发库均为 Debian 已有旧版本，来自签名软件源。复用已安装的 Python 3.12.14、Node.js 与 uv 引导工具。

## 后续更新

需要升级时，先将新截止时间设为发布日至少 7 天前，再重新解析锁文件、执行日期与哈希校验、审查锁差异、构建和运行测试。不要在服务器直接执行无版本约束的 `npm install` / `pip install -U`，也不要遇到镜像缺日期就关闭联网安装的年龄检查。更新核验报告后随代码一起提交。
