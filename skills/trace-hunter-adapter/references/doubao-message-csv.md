# 豆包消息级 CSV 导入

消息级 CSV 适配器版本为 `1.0.0`，目标协议为 `trace-hunter/1.1`。它与
`doubao-command-corpus@1.3.0` 是两个独立来源适配器：前者每行是一份回复样本，
后者按 conversationId 合并命令片段。不能混用身份和去重规则。

机器可读的 61 列契约位于
`contracts/imports/doubao-message-csv-fields-v1.json`。`source-audit.json`
根据同一契约记录每列的出现、NULL、空字符串、类型和最大长度，并检查嵌套
`meta / artifacts / mainagent` 结构。

## 导入

```bash
python3 scripts/trace_import.py inspect \
  --format doubao-message-csv \
  --source /absolute/source.csv \
  --report /absolute/source-audit.json

python3 scripts/trace_import.py prepare \
  --format doubao-message-csv \
  --source /absolute/source.csv \
  --output /absolute/new-bundle
```

输入和输出都必须使用新路径。prepare 会把原 CSV 按原字节复制到
`import/raw/source.csv`，并使用流式 SHA-256 校验，避免把多 GB 文件一次载入
内存。原始 CSV 是私有证据，不进入 Git。

## 身份

- 每个 CSV 行是一份回复 run，`enc_bot_message_id` 是来源身份；run ID 还绑定
  源 CSV 哈希和适配器版本。
- 精确相同的 `masking_parent_content` 共享 query_id；缺失时才回退到
  `prompt`。两者都缺失时生成该行独有的未知 query_id。
- `enc_conversation_id`、`enc_section_id`、`enc_message_id`、`enc_log_id` 和
  `message_index` 保存在 `record-index.json` 与 evidence，不用于跨行删除。
- env_id 只由明确观测到的 platform、local computer mode、app/mode 和客户端
  版本组成。local computer mode 不等同于已证明的 sandbox，联网状态也保持
  unknown。

## 内容和状态

标准 run.query、Case prompt 和最终回复优先使用 masking 字段。原值与全部 61
列仍在 raw CSV。展示字段按协议上限截短，原始长文本不改写。
来源 products / uploaded_attachments 作为 artifact evidence 保留；若单项 evidence
超过协议上限，只写数量与内容哈希，完整元数据继续由 raw CSV 提供。

`meta.status=error` 映射 failed。completed 且发生中断或没有最终回复时映射
partial，其余 completed 保持 completed。工具状态只接受完整结构化响应中的
显式 `ok` 或明确的进程退出包装；不扫描普通正文中的 error/失败关键词。

## 累计快照和调用

`message_info_list_final.mainagent.messages` 包含运行时提示、历史会话和重复快照，
不能把所有 role 记录都当成新调用。

- 带 `history_session` 或 `history_round` 的消息只保留在 raw。
- 模型 span 来自当前 assistant 记录中的逐请求 token_usage；相同 agent、时间、
  duration、usage 和内容的快照只导入一次。
- 工具 span 必须有 tool_call_id。同一 ID 的调用与返回配对；增量快照冲突时，
  选择带时间且内容更完整的版本，并在 evidence / record-index 中记录冲突数。
- tool_call.start_time 是开始边界，配对 tool_result.start_time 是结束边界；
  duration_ms 独立保留。冲突值不强行合并。
- organizer/subagent 树按各自 agent_id 导入，但不根据名称猜测父子模型请求。

行级 `model_call_cnt` 和 `token_usage` 是聚合口径。只有逐请求事件数量和 token
求和都与聚合值一致时，model_requests coverage 才是 complete。其余情况保留
聚合值、可归属和差额，标记 partial 或 missing，不将差额平均分摊到伪造请求。
工具 coverage 同样要求声明数量、当前 tool_call_id 和配对结果一致。

## 当前 6,000 行验证集

2026-09-11 提供的文件实际包含 6,000 行、2,428 个 conversationId 和 4,807 个
精确 prompt 组。61 列齐全，嵌套 JSON 解析错误为 0。流式规范化得到 57,695 个
可归属模型 span 和 96,774 个工具 span；6,000 份 trace 全部通过协议校验，
最大单份约 798 KiB，没有超过 16 MiB。

模型逐请求覆盖完整 471 行、部分 5,527 行、缺失 2 行；工具覆盖完整 3,253 行、
部分 2,243 行、缺失 504 行。这些差异是来源可见性结果，不通过补造调用消除。
