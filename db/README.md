# PostgreSQL

`migrations/*.sql` 按文件名顺序执行，版本和 SHA-256 保存到 `schema_migrations`。已应用的 SQL 不应修改；新变更追加迁移文件。执行入口为 `scripts/migrate_database.py`，连接由 `DATABASE_URL` 提供。

旧表包括 `runs`、`collections`、`case_definitions`、`collection_cases`、`analyses`。新版本资源使用 `trace_heads` / `trace_revisions`、记录索引、项目服务身份以及 `selection_snapshots` / `selection_members`；完整原件和选集清单由 ContentStore 按摘要保存。原始 JSON 以 TEXT 或原件字节保真，派生索引不改变 payload 和 digest。

迁移 009 引入 `trace_revision_lookup`。项目 namespace 与查询元数据在新 revision 的同一事务登记；升级时从已保存 metadata 分页回填查找表，并补齐旧 namespace 登记。查询不自动修复。查找表缺行导致 503 时，可重新运行显式迁移命令回填，已有原件/版本/清单不变。查找值采用无损 UTF-8 编码，保留空串、NUL、null 的差别；索引前缀不会替代完整相等比较。

从旧 SQLite 的五表快照迁移（旧兼容流程）：

```bash
.venv/bin/python scripts/migrate_database.py --sqlite /path/to/consistent-backup.sqlite --report var/migration.json
```

此 `--sqlite` 流程仅复制并核对上述五张旧表，不迁移新增的版本、身份、选集和内容目录；它不是当前平台的完整备份恢复工具。新资源的跨库迁移与部署验收列在第 036 轮；当前新服务应直接使用 PostgreSQL，并备份数据库与共享内容目录。五表复制遇到不同的既有目标数据会拒绝迁移。正在运行的 SQLite 必须使用 backup API 生成快照；正式切换时先停止旧服务写入，再生成最终快照。远端完整流程见 `docs/deployment.md`。
