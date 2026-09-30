# API

从仓库根目录启动，Python 3.12。API 与前端分别运行。

```bash
uv venv --python 3.12 .venv
uv pip sync --python .venv/bin/python --require-hashes --only-binary :all: apps/api/requirements.lock
export DATABASE_URL='postgresql://trace_hunter:YOUR_PASSWORD@127.0.0.1:55432/trace_hunter'
.venv/bin/python scripts/migrate_database.py
ALLOWED_ORIGINS=http://127.0.0.1:5173 .venv/bin/python apps/api/run.py
```

`DATABASE_URL` 必填；生产使用 PostgreSQL，本地验证可显式指定 SQLite 文件路径。`API_HOST` 默认 127.0.0.1，`API_PORT` 默认 8767；OTLP gRPC 默认随主服务监听 `127.0.0.1:4317`。`ALLOWED_ORIGINS` 为逗号分隔的完整来源白名单，部署时填写实际前端地址。来源检查不是用户认证。

健康检查为 `/api/health`，共享契约为 `/api/openapi.json`。普通导入最多 16 MiB；更大 Adapter 来源使用 `/api/v1/projects/{project_id}/imports/uploads` 分块续传。OTLP HTTP 使用 `/v1/traces`。事实校验与不可变存储由共享领域层处理。

设置 `TEST_DATABASE_URL` 为专用测试库后，运行 `.venv/bin/python -m unittest discover -s tests -v`。不要把数据库连接串写入源码、截图或公开日志。
