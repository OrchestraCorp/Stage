---
title: P1b 身份管理、认证与授权实施入口
status: implementation-planning
implementation_status: not-started
review_date: 2026-10-25
policy_effect: none
---

# P1b 身份管理、认证与授权实施入口

P1a 提供组件运行、PG/Redis 资源、短事务命令与可靠投递基础。P1b 将真实认证、持久身份事实和当前授权接入同一执行链。本文整理实施顺序，不把候选 IdP、客户端拓扑或 API 名称当作已冻结协议；详细要求继续以[身份规范](identity-and-authorization.md)和[一期方案](phase-1.md)为准。

## 1. 三层责任

| 层 | 回答的问题 | 交付 |
|---|---|---|
| 认证 Authentication | 这次请求的凭据由谁签发、代表哪个主体？ | 受信任 OIDC/OAuth profile、Stage access-token 验证、AuthenticationEvidence、主体映射 |
| 身份与授权事实 | 主体属于哪个公司、当前担任什么岗位、代表谁？ | Principal、ExternalIdentity、TenantMembership、RoleAssignment、ServiceGrant、有限 Delegation 及版本/撤销 |
| 授权与强制执行 Authorization | 当前主体对这个对象能执行什么？ | PostgreSQL 可信 loader、RBAC/ReBAC/ABAC、统一命令与读投影、提交前重验和审计 |

IdP 管登录凭据和会话；Stage 管组织事实与业务许可。Maestro 在本地负责客户端登录交互和选择上下文，云端 Stage 验证面向自己的 access token。用户提交的 tenant、任职或委托引用仅用于选择，必须由服务端验证归属与当前状态。

## 2. 当前代码接缝

- `PrincipalTable` 仍为最小主体表；任职/关系模型缺新身份合同要求的 tenant、稳定业务引用、来源和 revision，需要前向迁移，不能直接把测试 loader 用于生产。
- `AuthorizationEngine` 已有单任职与有限 Agent 委托判定；service 仍走 assignment 路径，必须新增独立 ServiceGrant 输入，不给 worker 虚构人类任职。
- CommandService 已按调用取得 Core 存储 lease。真实鉴权在应用执行层构造上下文，Core 不接收用户 token 或角色。
- 当前 Kernel 先锁 tenant guard 再调用 loader；接入全局主体/信任状态前须按统一锁序改造协调层：TrustState → 全局主体 → tenant guard → 领域对象。IdP 网络调用不得占用数据库锁。
- 当前默认 HTTP 只有系统探针。新增受保护接口时同步 OpenAPI；测试 fixture 不作为公开业务端点或真实协议失败的兜底。

## 3. 实施顺序与退出依据

| 切片 | 交付 | 验证 |
|---|---|---|
| P1b.1 协议与主体模型 | 固定一个真实 token profile、隔离测试 IdP、Stage audience、主体映射规则；建立 tenant/membership/assignment/service-grant 等最小持久结构与 bootstrap | 迁移与约束、可信 issuer/sub 映射、跨 issuer 同 email 不自动合并、重复初始化不增权 |
| P1b.2 真实认证链 | 真实授权码＋PKCE 测试客户端、access token 验证、workload 独立注册、受保护的当前身份/上下文读取切片 | 正确凭据通过；错误 issuer/audience、过期/未生效、坏签名、ID token 混用、伪造身份头拒绝；未知 key 刷新有界 |
| P1b.3 当前授权 | 可信数据库 loader、选定任职、资源关系、service grants、不可转委托的 Agent 委托；统一读/写强制入口 | 两租户隔离、scope/资源/主体不匹配拒绝、禁止岗位并权和 Agent 权限拼接；结果读取也重验 |
| P1b.4 撤销与客户端契约 | 共同锁序、撤销版本、到期与队列重验、稳定错误和最小审计；供本地 Maestro 对接的 OpenAPI/示例 | 两个真实 PG 连接验证撤权与提交竞争；401 携带适当 Bearer challenge，403/资源隐藏策略不泄露其他租户；新旧上下文不串用 |

第一条身份读取切片只返回当前调用者获准查看的主体、租户和任职投影；端点命名在实现前固定。它用于证明认证→映射→成员核验→最小投影贯通，不提前建完整人员管理后台。

## 4. 实现前需固定的最少选择

1. **本地测试 IdP 与实际 token profile。** 可优先评估 Keycloak 的标准 OIDC/OAuth 路径；固定可取得版本和真实 access-token claims 后再声明支持。不能假设所有 JWT 都满足 RFC 9068，也不同时实现未使用的 opaque profile。
2. **Maestro 的首个登录拓扑。** Desktop 可用系统浏览器授权码＋PKCE，再调用 Stage；Web/BFF 的会话存储与回调需独立约定。先确认本地产品入口，Stage 不把未知 origin 或回调加入默认信任。
3. **受信任配置与初始化。** 冻结测试 issuer/audience/client、算法/JWKS、初始虚构主体/租户及任职或 service grant；采用受控 bootstrap，首次登录不自动成为管理员。

上述是实施准备与候选方向；生产 IdP、KMS 和 Passkey 批准方案不在本轮选定。OIDC/OAuth 使用成熟实现，Stage 自研的是组织身份与执行语义。

## 5. 阶段验收边界

按原规格验收 I1-01～25、I1-35～36 的适用项；未支持 profile 明确拒绝，未运行正向场景不能计为通过。签名批准/Passkey 与 WASM 相关 I1-26～34 在 P1c/P1d 继续。Redis 失效或缓存命中均不能替代 PostgreSQL 当前授权；原 token 不写队列、审计或通用缓存。

完整任务业务、人类最终验收及职责分离按已有 P2 规格推进；P1b 要提供能够承载这些规则的可信主体、关系与授权输入。本地 Maestro 的 UI 与模型配置保持独立研发，通过版本化 API 合同对齐。
