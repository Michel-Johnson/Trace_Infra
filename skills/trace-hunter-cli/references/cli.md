# Trace Hunter CLI 参考

下载包内的 CLI 位于已安装 Skill 的 `trace-hunter-cli/scripts/trace_hunter_cli.py`，只依赖 Python 标准库。以下示例从该 Skill 目录执行；仓库开发时也可使用根目录兼容入口。官方测试服务配置见 [service.json](service.json)，当前无需用户名和密码：

```bash
python3 scripts/trace_hunter_cli.py \
  --url http://10.37.24.3:8766 --project benchmark-a COMMAND
```

默认从 `TRACE_HUNTER_URL`（未设置时为 `http://127.0.0.1:8766`）和 `TRACE_HUNTER_PROJECT` 读取连接。输出为 JSON；失败也输出 JSON 到 stderr。退出码：`0` 成功、`3` API 4xx、`4` API 5xx/网络错误、`5` 本地输入错误。

判断调用是否成功时先检查 CLI 自身退出码及 JSON 的 `error/status`。不要用 `2>&1 | head` 或无 `pipefail` 的 `| jq` 判断结果：管道默认只返回末端命令的退出码，会把参数错误、HTTP 422 或网络失败误报为成功。确需管道时先启用 `set -o pipefail`，且不要仅凭空输出认定成功。

```bash
.venv/bin/python scripts/trace_hunter_cli.py profile set cloud \
  --url http://10.37.24.3:8766 --project benchmark-a --timeout 300
.venv/bin/python scripts/trace_hunter_cli.py profile use cloud
```

Profile 保存 URL 和项目。只有其他服务明确启用认证时，Token 或密码才从环境读取；`--profile local` 临时使用本机服务。

## 能力与项目

```bash
.venv/bin/python scripts/trace_hunter_cli.py capabilities
.venv/bin/python scripts/trace_hunter_cli.py project list
.venv/bin/python scripts/trace_hunter_cli.py project get PROJECT
.venv/bin/python scripts/trace_hunter_cli.py project create PROJECT "Display name"
```

## 导入与版本

豆包 `case_id/raw_turn/calls` 原始导出先转换为 v2 包：

```bash
.venv/bin/python scripts/trace_agent.py prepare raw.json --format doubao-turn-export \
  --output bundle --run-id RUN --query-id QUERY --env-id ENV
.venv/bin/python scripts/trace_agent.py check bundle
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT import bundle/trace.json
```

不要直接导入 `raw.json`；`source.json` 是原始证据，`trace.json` 才是平台输入。

```bash
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT import trace.json
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT trace history RUN
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT trace get RUN REVISION
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT trace content RUN REVISION
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT trace index RUN REVISION
```

`import` 默认用文件 SHA-256 生成稳定幂等键。补采或修正必须显式传 `--expected-previous` 和对应 `--derivation`，不能随机换键绕过冲突。

已注册 Adapter 的原始来源可以直接启动可观察导入；`--wait` 会输出 NDJSON 状态并在结束后退出：

```bash
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT upload raw.json \
  --source-format ATIF-v1.8 --run-id RUN --query-id QUERY --env-id ENV --wait
```

大小文件统一使用可续传上传；指定已注册格式不会启动 Agent。旧 adapter-import 命令保持兼容，但内部使用相同上传协议：

```bash
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT upload raw.json \
  --source-format agent-benchmark/1 --run-id RUN --query-id QUERY --env-id ENV --wait
```

未知格式或网页同款自动流程直接执行 `upload raw.json --wait`：CLI 先初始化当前 Agent 会话 cookie，再上传分块，服务器启动 Agent 识别格式并选择或开发 Adapter。重复上传同一项目、文件名和字节会返回同一任务；任务失败后使用 task retry TASK_ID 显式重试，task cancel TASK_ID 取消；任务详情由 task get TASK_ID 获取。`import-adapters` 列出当前项目保存的脚本；`import-adapter-download SHA256 OUTPUT` 校验摘要并写入一个不存在的目标文件。

批量上传同时传入 `--batch-id BATCH --batch-total TOTAL`，任务与批次进度将同步到网页。

## 后台任务

评测或长分析由 Agent 执行时，使用统一任务让网页同步展示状态：

```bash
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT task create evaluation "评测标题" \
  --request-key STABLE_KEY --body '{"steps":[{"id":"collect","label":"读取证据"},{"id":"score","label":"评分"}]}'
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT task update TASK_ID \
  --body '{"state":"running","current_stage":"collect","progress":0.5,"processed":1,"total":2,"message":"证据读取完成"}'
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT task watch TASK_ID
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT task list --state running
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT task list --cursor NEXT_CURSOR
```

`task watch` 使用后端 SSE，断开后可用 `--after REVISION` 续读。`task list` 的 `next_cursor` 用 `--cursor` 续页，并检查 `retention_limit`；大批次总量以导入批次摘要为准，不用最近任务条数代替。任务进度必须来自真实检查点；异常时写入 `failed` 与结构化 error，不能停留在 running。快速查询不创建任务。

## Trace 查询与聚合

以下最小请求的结构已按服务契约验证；将 `RUN_ID` 替换为项目内真实 ID。`trace` 没有 `list` 子命令；分页大小使用 `--limit`，不是 `--page-size`：

复杂请求可直接传 JSON，或用 `@path`：

```bash
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT trace query \
  --body '{"filters":{"run_id":["RUN_ID"]},"fields":["run_id","revision","status"],"limit":1}'

.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT trace query --all-pages \
  --body '{"filters":{"status":["failed"]},"fields":["run_id","revision","status","index_state"]}'

.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT trace aggregate \
  --body '{"group_by":"model","revisions":"latest"}'
```

## Span、Skill 与耗时

```bash
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT span \
  --body '{"filters":{"run_id":["RUN_ID"],"kind":["tool"]},"fields":["run_id","revision","span_id","kind","status"],"limit":1}'

.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT span --all-pages \
  --skill-name lark-cli --fields run_id,revision,span_id,duration_ms,source_refs \
  --order duration_desc

```

可重复或逗号分隔使用 `--run-id`、`--skill-name`、`--skill-action`、`--name`、`--operation`、`--status`。耗时边界使用 `--min-duration-ms` / `--max-duration-ms`。未覆盖的高级字段通过 `--body` 传入。
`span` 没有 `--kind` 参数；对象种类写在 `--body` 的 `filters.kind` 中。`run_id` 也必须放在 `filters.run_id`，不能作为请求顶层字段。

以某个 Span 为锚点读取连续邻域，并在同一次请求中展开正文投影、关联对象和关系：

```bash
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT span-window RUN REVISION SPAN \
  --before 50 --after 20 --include documents,related_objects,edges
```

每侧最多 100 个 Span；返回顺序依据是 `source_ordinal`，不能自动解释为因果。正文为有界预览，继续读取前先检查 `preview_truncated`、`text_state` 和 `attachments_truncated`。

## 文本搜索

```bash
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT search 'lark-cli' --mode literal --all-pages
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT search 'tool' --evaluation --all-pages
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT search-terms
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT search-term-promote tool
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT search-term-demote tool
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT search '/docs/[^ ]+\.md' \
  --mode regex --field input,arguments --span-id SPAN_A,SPAN_B --all-pages
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT search 'error|failed' --mode regex
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT search '\ytool\y' --mode regex
.venv/bin/python scripts/trace_hunter_cli.py --project PROJECT search '\btool\b' \
  --mode regex --regex-syntax portable
```

过滤项：`--run-id`、`--span-id`、`--field`、`--object-kind`。`literal` 与 `regex` 返回精确 `match_ranges`。
`--evaluation` 只用于事后评测分析的字面量查询；后台按首次请求统计，翻页不重复计数。候选词不会自动晋升，明确选择后才为该词建立项目内精确子串索引。

正则的正式方言是 [PostgreSQL ARE](https://www.postgresql.org/docs/18/functions-matching.html#FUNCTIONS-POSIX-REGEXP)：运行前先查看 `capabilities` 的 `regex_dialect`、`regex_examples`、`regex_max_chars`。常用 `.`、`[]`、`|`、`()`、`(?:)`、`* + ? {m,n}`、`^ $` 可用；默认区分大小写。词边界是 `\y`（词首 `\m`、词尾 `\M`）；ARE 中 `\b` 表示退格字符。若输入习惯来自 Python/JavaScript，可显式加 `--regex-syntax portable`，当前仅将字符类外的 `\b` / `\B` 转为 `\y` / `\Y`；响应 `effective_pattern` 表示实际执行的表达式，不承诺兼容整套 Python/JavaScript/PCRE 语法。Shell 中用单引号保留反斜杠；直接构造 JSON 时按 JSON 规则转义。

为控制复杂度，模式最长 256 字符，拒绝环视、反向引用和分组后紧跟 `*`、`+`、`{`。错误模式会失败而不是退回 literal；过宽查询可能超时，应先用结构化过滤缩小范围。PostgreSQL 对命中和片段范围使用同一种 ARE 语义；SQLite 仅用于测试回退，不是正式正则执行环境。

所有分页查询都应优先用 `--all-pages`。如需自行续页，传响应中的 `next_cursor`；改变查询条件后不能复用旧 cursor。

## 对象证据、指标和批量导出

对象过滤条件放在 filters，正文选择 payload 字段，使用 --all-pages 完成分页。首次响应的 snapshot 绑定查询水位；后续必须保持相同查询条件。

    python3 scripts/trace_hunter_cli.py --project PROJECT object-query --all-pages --body @query.json
    python3 scripts/trace_hunter_cli.py --project PROJECT metrics-query --body @metrics.json
    python3 scripts/trace_hunter_cli.py --project PROJECT evidence-export --body @export.json
    python3 scripts/trace_hunter_cli.py --project PROJECT task watch TASK_ID

对象请求示例：

    {"filters":{"run_id":["RUN_ID"],"revision":[1]},"revisions":"all","fields":["run_id","revision","object_id","object_kind","payload","source_refs"],"limit":100}

指标请求示例：

    {"query":{"filters":{"object_kind":["span"],"kind":["tool"]}},"metrics":["count","error_rate","p95"],"group_by":["name"]}

导出 body 需有稳定 request_key 和 query；成功后从 GET /api/v1/projects/{project_id}/evidence-exports/{task_id}/content 下载。只使用已授权证据范围。超过指标查询预算时明确返回 partial/unsupported，或按已批准的固定样本分组计算；不能平均分片分位数冒充全量分位数。
