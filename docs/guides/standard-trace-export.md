# 官方接入指南：把完整执行转为标准轨迹

目标读者：harness/SDK 开发者、轨迹转换程序与负责接入的 agent。本文对应[平台目标设计](../architecture/platform-contract-and-plugins.md)，v2 尚未上线。字段以版本化 Schema 为准，指南不替代 Schema。

## 1. 确定执行身份，任务关联可以后补

平台或 SDK 为一次执行生成 run/document 身份；不能把供应商 session_id 猜成任务 ID。中性导入不要求 query_id/env_id、评测集或评分信息，未绑定可留空。运行标准评测时，再取得 query_id + Case revision、env_id + 环境 revision 和运行计划，并按要求固定初态、harness/config、工具及技能版本。

直接导入已有日志时，可创建独立 Run，缺少的计划或环境证据显式标 unknown。题目文本是 Case 输入，query_id 是稳定身份；标题只是展示名。所有样本明确 real 或 synthetic，合成数据不能进入真实模型性能报告。

## 2. 在发生处采集

- 模型请求发出前保存实际 payload，结束时保存响应与 provider usage，失败重试各有 attempt；同一次流式请求的多个片段共享请求身份。
- 工具执行前后保存参数、结果、错误和时钟；模型提案与实际执行通过 ID 关联。技能标注在真实操作上，不凭名称假造执行。
- 用户输入产生实际 turn，重复进入上下文的历史消息保持原身份。请求实际输入与聊天消息池分别记录。
- 记录压缩、恢复、分支与中断及其上下文/检查点引用。分支与重跑新建 Run；同一运行恢复增加 segment。
- 保存初态、终态和业务产物；大内容按摘要引用。采集原始请求的可见内容，不要求或推断模型没有公开的内部推理。

逐请求使用明确的时间边界；模型响应时间与工具时间分别记录。缓存/思考用量是总量的子项。保留原始 usage、原始时间与转换口径，禁止为了“完整”补零或拼造上下文。

## 3. 按标准整理

标准包逻辑上由任务/环境版本引用、运行元数据、TraceDocument、来源/附件和转换报告组成。调用、消息、请求上下文、事件、产物归各自对象。平台生成索引，转换器不生成评分。

单条 Trace 内通过 turns[] 表示多轮，spans 关联 turn_id。批量导入用 items[] 包装多个独立 Trace，可只导入一条，也可混合多个 query，不要求导入全部题目。任务/环境定义属于可选关联，见[中性导入规则](../architecture/neutral-trace-import.md)。

适用域没有事件时声明 complete + recorded_count=0 + expected_count=0；未采集用 missing，无法确定用 unknown；被截断用 partial；隐藏用 redacted；确实不适用附理由。每个字段缺口定位到 record_id / JSON Pointer。完整采集是默认目标，部分轨迹仍可按实际能力导入。

版本/标签分别保存：schema、Case、环境、harness/config/prompt、tool/skill、collector/adapter。tags 为字符串集合，labels 为可筛选键值；厂商复杂字段放 `vendor.feature` 命名空间，不覆盖核心字段。

## 4. 校验后导入

分四层验证：JSON Schema；ID/引用/尝试/时间/计费语义；内容摘要与可读性；profile/插件的数据要求。前两层有身份冲突时修正 adapter；后两层缺数据时保留缺口，不删除记录。

服务端使用同内容幂等重试；同身份不同内容报冲突。后补数据产生新 document revision。先取得导入 receipt，再查询 coverage；导入不触发评分。

目前可运行的离线检查入口：

```bash
.venv/bin/python scripts/trace_agent.py describe
.venv/bin/python scripts/trace_agent.py prepare examples/claude-orange.trace.json --output var/agent-import/claude
.venv/bin/python scripts/trace_agent.py check var/agent-import/claude --require browse
```

这三个命令是现有来源适配原型，并非完整采集 SDK；其支持格式由 describe 列出。平台 Schema/插件合成样例在 `contracts/drafts/platform-v1` / `examples/drafts/platform-v1`，尚不能直接发给线上 1.1 导入接口。

## 5. 新接入器的验收

提交支持的来源/adapter 版本及 fixture，至少验证：完整单轮、完整多轮、工具并行、重试、压缩/恢复、空工具集、缺失时间、缺失用量、未知工具、原文截断。核对请求和工具身份、源字段、原始字节摘要及统计归属；不能只用转换器自己的输出来证明正确。

输出紧凑转换报告：支持范围、完整性、问题的 code/path/reason/fix、内容引用和不能还原的字段。agent 可先查询摘要、再读取相关切片，不把全部原文放进工具返回。可按本指南制作 skill；将查询与转换交给工具，skill 只保留流程和判断规则。
