# 替换同一批会话的标准化版本

用于已确认修复包与线上集合一一对应的维护操作。保留集合、Case 定义和编号，使用新 run ID 导入修正版；旧输入和派生产物移出当前数据库，存入私有备份。此操作不属于日常导入，不会自动触发插件。

## 范围与检查

`scripts/replace_collection_runs.py` 提供 `plan` 和 `apply` 两个入口。数据库连接从 `DATABASE_URL` 环境变量读取，不接受命令行密码。

- 明确指定集合、旧/新采集器版本和预期数量。每个目标 Case 必须恰好对应一条旧运行和一条新运行；新 ID 不得已存在。
- 验证 ready manifest、导入文件大小与 SHA-256、协议和身份。现有集合的 Case 集合必须与修复包完全一致；包中的新 catalog 不导入。
- 仅允许运行 ID、采集器版本、调用输入/输出/状态与新增证据发生变化。原始来源、已有证据、记录 ID、时间、顺序、环境等必须保持。
- 存在排队/执行中的目标任务，或集合分类请求混有其他运行时，拒绝替换。
- `apply` 再次检查文件和数据库快照，并在 PostgreSQL 表锁或 SQLite 写事务内完成替换。任何检查或写入失败都会回滚事务。

## 操作顺序

1. 用 `trace_import.py verify --bundle` 校验输入包和源文件。
2. 生成计划：

   ```bash
   python scripts/replace_collection_runs.py plan \
     --bundle /private/normalized-bundle \
     --collection existing-collection-id \
     --old-version 1.1.0 --new-version 1.2.0 \
     --expected-count 50 --plan-file /private/replacement-plan.json
   ```

3. 核对计划摘要，暂停 API 与 worker，保存并验证完整 PostgreSQL dump。保持数据库运行。计划及 dump 含原始数据，必须保存在受限目录，不能提交仓库。
4. 执行替换；`--backup-dir` 必须是尚不存在的新目录：

   ```bash
   python scripts/replace_collection_runs.py apply \
     --plan-file /private/replacement-plan.json \
     --backup-dir /private/backups/replacement/affected
   ```

5. 无论成功或失败，都恢复原先运行的服务。确认健康检查与登录保护。
6. 成功后核对新运行摘要、其他运行及产物摘要、原 Case 编号；按用户要求显式触发新数据的分类，检查完成数量和错误统计。

计划后若数据库改变，重新检查并生成新计划；不要绕过快照检查。`affected-rows.json` 和摘要清单会在首次数据库修改前完成落盘。清单的 `prepared` 表示归档已完成，不代表数据库已提交；保存 `apply` 返回结果与后续验证记录作为完成凭据。

## 恢复

事务内失败无需恢复旧库。已提交后如需撤回，先核对备份和线上后续变化，在隔离库恢复 dump 并制定范围明确的恢复操作；不可直接覆盖已有新导入的生产数据库。备份保留被移除的旧运行、分类/评分任务及结果、集合派发记录和对应 Case 定义，可用于审计和定向恢复。
