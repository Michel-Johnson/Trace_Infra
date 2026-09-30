# 项目与 Agent 服务身份

状态：第 006 轮已实现并通过隔离环境验收，尚未部署。新 `/api/v1` 资源支持项目范围的服务身份；旧集合、Case 和插件接口仍处于原先的单账号管理边界，不因此成为多租户接口。

## 使用流程

1. 管理员创建项目，再为远程 Agent 创建 service principal。
2. 管理员签发有效期 1 秒至 90 天的凭据。明文只在签发响应出现一次；数据库只保存高熵随机凭据的 SHA-256。
3. Agent 使用 `Authorization: Bearer <credential>` 调用项目接口，每次请求重新校验身份、过期和撤销。
4. 管理员可以单独撤销凭据或撤销身份及其全部凭据。重新签发可用于轮换；历史记录保留。

| 权限 | 允许操作 |
|---|---|
| traces:read | 读取项目内指定版本、原件与历史 |
| traces:write | 向项目追加原件和版本 |
| traces:index | 显式重建指定版本的查询投影 |
| selections:write | 显式冻结查询成员与版本（009） |
| artifacts:read | 查询项目内已有产物、描述与经校验正文（011） |
| artifacts:write | 显式提交不可变产物（011）；有输入时另需相应读取权限 |
| operations:read | 发现项目内操作版本和输入/输出合同（012） |
| invocations:write | 显式创建固定计算请求（012）；另须操作和上游读取权限 |
| invocations:read | 读取项目计算请求及状态（012） |

权限独立，write 不自动包含 read。其他项目、未授予的操作和旧管理接口均拒绝。普通服务身份不能创建项目、签发凭据或修改自己的权限。签发/撤销与并发操作使用同一身份行锁，禁用身份后不会再签发新凭据。已在执行中的请求不承诺因撤销而中止，后续任务执行使用单独的 attempt/租约机制。

## 管理接口

- POST/GET `/api/v1/projects`：创建/分页列出项目。
- GET `/api/v1/projects/{project_id}`：项目描述。
- POST `/api/v1/projects/{project_id}/principals`：创建身份，输入 name 和 scopes。
- GET `/api/v1/principals/{principal_id}`：身份描述。
- POST `/api/v1/principals/{principal_id}/credentials`：签发，输入 ttl_seconds。
- GET `/api/v1/credentials/{credential_id}`：凭据描述，不返回秘密。
- POST `/api/v1/credentials/{credential_id}/revoke`：撤销凭据。
- POST `/api/v1/principals/{principal_id}/revoke`：撤销身份与全部凭据。

显式项目创建接口要求不超过 128 字符的 ASCII URL 安全标识；原先 trace_heads 中已有的项目 ID 原样登记、保持可读，不重写轨迹。为兼容已有导入约定，管理员直接导入新 namespace 时，在同一事务登记项目，初始显示名使用其 ID；追加失败时一并回滚。010 迁移也登记此前已接受但遗漏的 namespace，不覆盖管理员设置的名称。普通服务身份仍只能写其已获授权项目。项目名为显示文字。项目列表有界分页，使用固定字节顺序；单个项目的失败索引或缺失正文不影响身份记录。

## 部署边界

正式网页入口继续使用 Caddy Basic Auth。只有新的 v1 Bearer 通道会直接交给 API，API 验证失败返回 401，绝不降级为管理员。旧任务的 Bearer 仍由原任务凭据机制校验，不与服务凭据通用。新服务凭据不能访问旧全局管理接口。

v1 的管理员兼容入口仅信任真实 socket peer，不信任客户端的 X-Forwarded-For 等转发头。默认可信来源只有 127.0.0.1/32、::1/128；`TRACE_HUNTER_OPERATOR_NETWORKS` 可以显式配置网关来源 CIDR。容器网关不是 loopback，需配置准确的可信来源，不能把整个私网默认当管理员。保留 API 私有监听/内部网络，不能把默认管理员兼容模式直接暴露公网。

`TRACE_HUNTER_REQUIRE_SERVICE_AUTH=true` 禁止 v1 管理员兼容入口，所有 v1 请求都必须携带服务凭据；凭据初始签发由受信的服务端管理代码完成。旧接口继续依赖原网关隔离，strict 不将旧接口改造成对外多租户 API。

时间使用 UTC RFC3339。历史迁移时间只规范化表示，不当作轨迹采集时间。读取或管理身份均不生成分析、回放或训练消费记录。
