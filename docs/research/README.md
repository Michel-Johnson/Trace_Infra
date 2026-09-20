# 开源对照调研

这条分支只放学习笔记，不改运行时代码。每份笔记对着本仓库已经定下来的契约写：`state` 是可恢复检查点，`span` 是两个 `state` 之间的短轨迹，环境层要冻住再恢复同一份文件和进程。

规格仍以 `docs/spec/` 为准。这里的结论不能偷偷改接口。

| 序号 | 项目 | 笔记 | 和本仓库的关系 |
|---|---|---|---|
| 1 | [Dressage](https://github.com/Accio-Lab/Dressage) | [dressage.md](dressage.md) | Agentic RL 训练框架。正交拆法值得抄；没有 VM 级 `CommitState` / `RestoreState` / `Branch`。 |
