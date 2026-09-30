# 中性轨迹导入：单条、多轮与批量

2026-09-10。目标协议草案；取代早期草案中“query_id/env_id 必须先绑定才能导入”的要求。线上旧 1.1 接口暂不改变。

## 两层列表

**一条 Trace 是一次执行的记录，内部可以有一个或多个用户轮次。一次 Import 是运输批次，可以包含多条互相独立的 Trace。**

```text
ImportBatch
└── items[]
    ├── item_id: item-1
    │   └── trace: Run A
    │       ├── turns[]：用户轮次
    │       ├── spans[]：模型/工具/等待执行，通过 turn_id 关联
    │       └── messages[] / contexts[] / events[] / artifacts[]
    └── item_id: item-2
        └── trace: Run B
```

Trace 不是一个消息数组。单轮完整轨迹有一个 turn，多轮完整轨迹有多个 turn；每轮可含多个模型请求和工具调用。`turns[]` 保留采集到的轮次顺序，span 通过 turn_id 关联，公共初始化或未确定归属可留空。并发和打断由时间/依赖补充表达，不能用列表位置推断执行不重叠。

query_id 代表逻辑任务，可以对应整个多轮 Case，不是每条用户消息一个 query_id。同一个 query 的不同模型、harness 或重跑是不同 Run。任务内澄清仍属于原 Run；从头重跑新建 Run；恢复通过 segment 表达，新的导出快照通过 document revision 表达。缺少 turns 的旧数据标相应 coverage，不自动当单轮。

## 单条与批量使用同一入口

```ts
interface ImportBatch {
  schema_version: "trace-hunter-import/1.0-draft.1";
  items: Array<{
    item_id: string;       // 此批次内的结果关联 ID
    trace: TraceDocument; // 自带 run/document 身份
  }>;
}
```

items 长度为 1 即单条导入；可以批量导入同一 query 的多次运行，也可以混合多个 query 的运行，不要求题库、Case 或评测集全部完成。原生单轨迹文件可由 UI/SDK 包成一个 item。批次顺序只用于关联回执，不用于拼成一场会话。

草案最多 1000 项；实际 HTTP 服务还需限制总字节/单项字节/附件大小。大批量分批提交即可，不把所有 query 原文放进一次 agent 工具输出。实时追加同一 Run 的采集记录属于未来 capture API，不复用 items 假装每批事件是一条新轨迹。

传输信封格式错误整包拒绝；信封可读后，每项独立校验和提交，回执包含 item_id、imported/unchanged/rejected、run_id/document_id 及错误路径。一个坏条目不回滚已成功条目。相同文档同内容重传 unchanged，同身份不同内容 rejected；不自动合并两条轨迹，不用 item_id 代替持久幂等身份。当前离线 Schema 检查会列出不合法项，逐项持久化/回执仍是服务实现工作。

## 中性意味着什么

- 必需的是可识别的执行与文档身份、记录结构、来源与采集声明。SDK 可生成内部 run/document ID，但不能伪造 provider 请求 ID。
- query_id/env_id 是可选关联，未绑定可省略或 null。原始用户输入保留在 messages/context，即使没有 Case 也能浏览。实际环境观测保留，未知则用 unknown；不要求提交标准 EnvironmentSpec。
- 不要求 benchmark、Case 定义、oracle、评分、插件或训练标签。导入只做结构/引用/来源等事实校验，不评价模型好坏，不执行轨迹中的命令。
- 查询和可视化可直接使用未绑定轨迹，放在“未关联轨迹”入口；之后通过独立 binding 关联 Case、评测集和环境要求，不修改原始文档 digest。来源自带的 query/env 是保留的声明，平台关联关系单独存储。
- 缺时间、用量或业务产物不阻止中性导入；非法结构、冲突身份仍拒收。评分时才应用插件 requirements 和标准运行条件。

平台数据库需增加 `run_bindings` / 评测集成员关系，与不可变 trace_documents 分离；绑定变更有修订和审计。比较按当前选定绑定及 Case revision 分组，保留源 query_id；不能靠文本相似自动认定同一个 Case。

机器定义：[批量导入 Schema](../../contracts/drafts/import-v1/import.schema.json)、[Trace Schema](../../contracts/drafts/trace-v2/trace.schema.json)。二者通过固定 URN 引用，离线验证器只注册本地 Schema，不自动从上传内容下载 Schema。平台元数据的 query_id/env_id 也允许 null；关联 Case/环境版本时必须与相应 ID 一致。

现有来源适配 CLI 仍保留其明确绑定参数要求，这是 adapter 原型的限制；新的中性导入不依赖那些适配器，也不要求更改原生已标准化轨迹。
