---
title: Maestro 多 Stage 连接与切换合同
status: accepted-design-client-implementation-pending
date: 2026-10-26
policy_effect: client-connection-isolation-and-instance-switching
---

# Maestro 多 Stage 连接与切换合同

2026-10-26 已确认：Maestro 可以保存多套 Stage 连接并允许用户显式切换；每套连接仍是独立的 Stage 部署、身份、会话和权限边界。该能力属于 Maestro 客户端连接管理，不把 Stage 改造成 SaaS 多租户服务，也不允许客户端在一个 Stage 请求中选择第二个组织根。

当前文档只落档 Stage 侧合同和客户端边界，没有修改 Maestro 仓库。Stage 继续保持[一实例一个组织授权根](organization-root-semantics.md)；Maestro 的实际界面、凭据存储和切换实现另行验收。

## 1. 目标与非目标

首版目标是：用户可以登记多个明确可信的 Stage endpoint，分别登录，查看当前实例的身份／任职，并在工作台中切换当前活动实例。每个活动工作区一次只面向一个实例，页面始终显示实例名称和环境。

首版不建设：

- 跨实例统一人员目录、账号合并、全局角色或共享权限；
- 跨实例搜索、统一收件箱、跨实例任务流或原子事务；
- 共享数据库、SaaS 租户选择、Stage 端的实例注册中心；
- 根据任意 URL 自动发现并信任新的 issuer；
- 把多个 Stage 的 token、任职、审计或授权结果合并成一个“当前组织”。

如果两个业务边界需要独立数据库、信任来源、审计责任或运维控制，就部署两个 Stage，由 Maestro 分别连接。若未来需要一个 Stage 承载集团、子公司或部门，应在该实例内部建设组织单元和明确 scope／关系规则，不通过客户端切换模拟组织树。

## 2. 客户端对象

Maestro 使用本地连接标识隔离状态；`connection_id` 只是客户端内部键，不是 Stage 的组织 ID、权限凭据或安全声明。

```text
ConnectionProfile
  connection_id       客户端本地不可变标识
  display_name        用户可识别名称
  environment_label   production / staging / development 等明确标签
  api_origin          精确 Stage API origin
  issuer              精确 OAuth/OIDC issuer
  client_id           已登记客户端
  redirect_uri        已登记精确回调
  profile_ref/version Stage 认证 profile 版本
  config_revision     本地配置代次

AccountSession
  connection_id + principal_ref
  该连接独立的 access/refresh 凭据和刷新互斥
  该实例的 membership / assignment / 管理状态

ActiveWorkspace
  connection_id + AccountSession + 一个选定 assignment
  当前页面、缓存、草稿和在途请求的实例归属
```

Stage 侧的 `organization_ref` 仍由服务端唯一根解析；Maestro 不把它作为连接切换参数提交。相同用户名、邮箱、`principal_ref`、`client_id` 或 `stage-api` audience 不能合并两个连接。两个实例可以把同一自然人映射为不同的本地主体和成员经历。

第一版连接配置使用部署管理员提供的受信任登记值即可：`api_origin`、`issuer`、`client_id`、精确回调和 profile/version 必须成组登记。后续如果确有需要，再增加签名的部署元数据发现；不能因为“支持多实例”就开放任意地址输入和动态 issuer 信任。

## 3. 登录与切换不变量

### 3.1 登录事务固定连接

发起授权时，Maestro 将 `state`、`nonce`、PKCE verifier、`connection_id`、issuer、client_id 和 redirect_uri 绑定到一次本地登录事务。回调到达时按原事务处理，即使用户期间切换了活动实例，也不能把 code 交给当前选中的另一连接。

客户端核验响应 issuer、ID token issuer/audience/nonce、精确回调和 state；不能按回调中的自报地址改变连接配置。Stage 当前 OAuth 元数据已声明 issuer 和授权响应 `iss` 支持，客户端仍需完成回调混淆和迟到响应验收。

### 3.2 请求固定连接

每个 API 请求在创建时捕获不可变连接上下文：origin、凭据、profile/version、principal 和 request id。不能通过修改一个全局 `baseURL`，让原来针对 A 的 Bearer 请求在等待后发往 B；也不能向未登记的跨 origin 重定向发送凭据。

活动实例切换只影响新建的页面请求。旧请求可以取消；若响应迟到，必须根据 request／connection 代次丢弃，不能覆盖新实例的页面。

### 3.3 会话、缓存和工具上下文隔离

查询缓存、分页游标、草稿、任职投影、管理能力投影、附件引用以及 AI／工具上下文都至少以 `connection_id + principal_ref` 隔离。切换后清理活动投影或换到另一连接的独立缓存域；退出或身份切换清理敏感内容。

工具调用在创建时固定实例和人员责任链。切换实例不会迁移、继续或重放原实例的工具调用；受监督工具仍通过原实例的业务 API 执行。

### 3.4 提交未知结果固定原实例

A 实例的提交出现 `COMMIT_OUTCOME_UNKNOWN`、网络超时或客户端取消时，Maestro 保留 A 的 origin、原幂等键、原输入、原任职和恢复地址。切换到 B 后不能在 B 查询或重放该操作，也不能用新键猜测 A 是否回滚。恢复仍需 A 的当前凭据和当前权限。

### 3.5 切换不等于退出

切换当前实例可以保留其他连接的 OS 安全存储会话；退出当前连接、移除连接配置、退出全部连接是三个不同动作。远端撤销失败时，客户端不能显示“已全部退出”；本地凭据清理和远端 session 状态分别展示。

## 4. Stage 与 Maestro 的责任

| 责任 | Maestro | Stage |
|---|---|---|
| 连接登记 | 保存展示名、环境标签、精确 origin/issuer/client/redirect | 只接受部署登记的 issuer、client、redirect 和 profile |
| 登录 | 为每个连接保存独立 state/nonce/PKCE 和凭据 | 验证 issuer、来源、client、会话、MFA 与当前主体 |
| 当前身份 | 连接级缓存和显示 | 返回本实例的 principal、organization、membership |
| 任职选择 | 只在当前连接显示和保存一个选择 | 每次业务请求重新验证当前任职 |
| 管理中心 | 当前连接的人员／权限页面 | 当前实例管理授权、状态、审计和撤权 |
| 切换 | 变更 ActiveWorkspace，隔离迟到响应和缓存 | 不提供跨实例组织选择参数 |
| 结果恢复 | 保留原 connection_id、原键和原输入 | 只在原实例按原人员和当前权限恢复 |

Stage 不需要增加跨实例全局身份表、共享 session 表或跨库锁。客户端支持多个连接也不会改变 Stage 的 PostgreSQL 权威事实、组织 S/X 锁序或单实例审计语义。

## 5. 首版验收

Maestro 实现前后至少验证：

1. A、B 使用相同用户名、相同资源 ID 和相同 `stage-api` audience；A 的 token、主体、任职和结果不能访问 B。
2. A 登录尚未完成时切换到 B，A 的回调仍只完成 A；迟到或错误 issuer 的回调被丢弃。
3. A 发起写入后出现未知结果，再切到 B；客户端仍只能在 A 使用原键恢复。
4. A 的慢请求在切换到 B 后返回，不能覆盖 B 的页面、分页游标或管理投影。
5. A 是管理员、B 是普通人员；切换后管理菜单和直接 API 请求都按 B 的当前权限处理。
6. 退出 A 不删除 B 的凭据；删除 A 连接不影响 B；退出全部连接清理本地所有会话并正确报告远端结果。
7. 生产、测试、开发环境在连接选择器、浏览器登录和错误页面中始终清晰可辨。
8. 切换期间不把 A 的草稿、附件、AI 上下文、工具调用或恢复地址带入 B。

Stage 服务端合同通过、Maestro 页面完成、真实桌面登录与切换验收是三个独立里程碑。当前 Stage 已支持独立 issuer、PKCE、会话、当前身份和任职边界；不据此宣称 Maestro 已实现多实例切换。
