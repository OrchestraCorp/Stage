---
title: P1b 身份管理、认证与授权实施入口
status: implementation-in-progress
implementation_status: p1b-first-slice-implemented-rest-pending
review_date: 2026-10-25
policy_effect: none
---

# P1b 身份管理、认证与授权实施入口

产品按企业私有化部署实施，不建设 SaaS 多租户选择和管理。`tenant_*` 是当前兼容存储名；一个数据库只有一个组织授权根和环境，根不等同于法人公司，集团/子公司等组织单元以后按需建模，部门/项目使用 scope。身份 v1 query 仅接受 `profile_ref/profile_version`，响应使用 `organization_ref`；旧未分版本路由移除。详见[组织授权根语义](organization-root-semantics.md)和[部署/API 边界](private-deployment-api-v1.md)。

P1a 提供组件运行、PG/Redis 资源、短事务命令与可靠投递基础。P1b 将真实认证、持久身份事实和当前授权接入同一执行链。本文整理实施顺序；首个公开身份接口已冻结为 `/api/v1/identity/me`，其他 IdP/profile 和客户端拓扑分别验收；详细要求继续以[身份规范](identity-and-authorization.md)和[一期方案](phase-1.md)为准。

2026-10-25 已确认[身份与授权表结构设计](identity-schema-design.md)，细化字段、复合外键、成员重入、角色版本、授权配对、来源准入、撤销并发及无损迁移。第一条真实认证→主体映射→部署组织/membership→最小只读 API 与受控 bootstrap 的纵向切片已实现并通过 PG17/真实测试 IdP 验证；完整角色/资源授权、ServiceGrant 和 Delegation 留在后续切片。实际范围与验证见[本轮实施记录](reviews/p1b-first-slice-2026-10-25.md)。

## 1. 三层责任

| 层 | 回答的问题 | 交付 |
|---|---|---|
| 认证 Authentication | 这次请求的凭据由谁签发、代表哪个主体？ | 受信任 OIDC/OAuth profile、Stage access-token 验证、AuthenticationEvidence、主体映射 |
| 身份与授权事实 | 主体是否为本企业当前成员、担任什么岗位、代表谁？ | Principal、ExternalIdentity、TenantMembership、RoleAssignment、ServiceGrant、有限 Delegation 及版本/撤销 |
| 授权与强制执行 Authorization | 当前主体对这个对象能执行什么？ | PostgreSQL 可信 loader、RBAC/ReBAC/ABAC、统一命令与读投影、提交前重验和审计 |

IdP 管登录凭据和会话；Stage 管组织事实与业务许可。Maestro 在本地负责客户端登录交互和选择任职/委托上下文，云端 Stage 验证面向自己的 access token。部署组织由服务端唯一根记录决定，客户端不能选择或覆盖；后续任职/委托选择仍须验证归属与当前状态。

## 2. 当前代码接缝

- 原 `PrincipalTable` 扩展版本/epoch，新增认证来源、tenant/membership 与 bootstrap 模型；旧任职/关系仍缺完整 tenant、稳定业务引用、来源和 revision，后续前向迁移不能直接把测试 loader 用于生产。
- `AuthorizationEngine` 已有单任职与有限 Agent 委托判定；service 仍走 assignment 路径，必须新增独立 ServiceGrant 输入，不给 worker 虚构人类任职。
- CommandService 已按调用取得 Core 存储 lease。真实鉴权在应用执行层构造上下文，Core 不接收用户 token 或角色。
- 原 Kernel 先锁 tenant guard 再调用 loader；当前身份读取与离线撤销已按 TrustState → 全局主体 → tenant guard 顺序协调，并通过 PG17 竞争测试。旧 Kernel 尚未接入前序共享锁，完整角色/领域提交必须继续改造后单独验收，不能复用身份读取的证明。IdP 网络调用不得占用数据库锁。
- 第一条受保护 HTTP 接口只暴露最小当前身份与部署企业成员投影，API 与 OpenAPI 同步；测试 fixture 不作为公开业务端点或真实协议失败的兜底。

## 3. 实施顺序与退出依据

| 切片 | 交付 | 验证 |
|---|---|---|
| P1b.1 认证与成员首条切片 | Principal 扩展、trust/provider/profile/企业准入、外部映射、tenant/membership、受控 bootstrap、固定 access-token profile 与最小只读 API | 迁移与约束、真实签名凭据验证、issuer/namespace/sub 精确映射、来源准入、成员当前性、重复初始化不增权 |
| P1b.2 任职与资源授权 | scope/resource、action/角色版本/任职、直接 ACL、可信 loader 与组织 IAM 变更；接入命令共同锁序及审计 | 单组织根约束、部门/项目 scope 与主体边界、任职固定成员经历/角色版本、scope/资源不匹配拒绝、禁止岗位并权、当前读写权限强制执行 |
| P1b.3 Service 与 Agent | workload/Agent 固定登记与绑定、独立 ServiceGrant、不可转委托的 Delegation 及逐条 terms | 主体类型与环境不能混用、动作—目标不交叉扩权、重入不复活旧授权、撤销/到期及队列重验 |
| P1b.4 真实客户端与完整竞争验收 | 隔离成熟 IdP、授权码＋PKCE、共同锁序与撤销竞争、供本地 Maestro 对接的 OpenAPI/示例 | 所选实际 profile 的适用 I1 项逐条留证；PG 多连接撤权竞争，401 Bearer challenge、403/资源隐藏、不同上下文隔离 |

第一条身份读取切片只返回当前调用者及其部署企业中的成员投影，不提前返回尚无当前角色 loader 保证的任职能力。它用于证明认证→映射→企业来源准入→成员核验→最小投影贯通；真实签名凭据的单元/集成测试与成熟 IdP 授权码＋PKCE 联调分别留证，前者不替代后者。

首批迁移采用增量扩展，保留旧主键及历史记录。旧任职/关系的缺失租户不从 scope 字符串猜测、不补默认公司；只要目标表仍有未映射旧行，就阻止最终约束收紧及完整角色 loader 切换。新独立身份读取入口只能访问已明确建立的新身份/成员事实，不借机加载旧任职。

## 4. 已固定的开发 profile 与客户端接缝

1. **本地测试 IdP 与实际 token profile。** 首条切片采用隔离 Keycloak 26.4.7 + OpenJDK 21，固定 `stage.keycloak-access.v1`：RS256、header typ=JWT、payload typ=Bearer、Stage audience、准确 issuer/client 与互斥的 `stage_credential_kind`。它不是 RFC 9068 或任意 JWT 通用实现，不接入尚未使用的 opaque profile；部署和具体边界见[本地 IdP 说明](../deployment/identity/README.md)。
2. **Maestro 的首个登录拓扑。** Desktop 可用系统浏览器授权码＋PKCE，再调用 Stage；Web/BFF 的会话存储与回调需独立约定。先确认本地产品入口，Stage 不把未知 origin 或回调加入默认信任。
3. **受信任配置与初始化。** 测试 issuer/audience/client、算法/JWKS、初始虚构主体与成员由离线开发 bootstrap 显式登记；首条切片不创建任职或 service grant，首次登录不自动成为管理员。CLI 仅接受 dev/test 并记录本机 OS 操作者，数据库变更和操作结果同事务，未知结果按原 key 查询。

上述部署参数按实际启用路径冻结并写入实施记录；未启用 profile 不宣称兼容。生产 IdP、KMS 和 Passkey 批准方案不在本轮选定。OIDC/OAuth 使用成熟实现，Stage 自研的是组织身份与执行语义。

## 5. 阶段验收边界

按原规格验收 I1-01～25、I1-35～36 的适用项；未支持 profile 明确拒绝，未运行正向场景不能计为通过。签名批准/Passkey 与 WASM 相关 I1-26～34 在 P1c/P1d 继续。Redis 失效或缓存命中均不能替代 PostgreSQL 当前授权；原 token 不写队列、审计或通用缓存。

完整任务业务、人类最终验收及职责分离按已有 P2 规格推进；P1b 要提供能够承载这些规则的可信主体、关系与授权输入。本地 Maestro 的 UI 与模型配置保持独立研发，通过版本化 API 合同对齐。
