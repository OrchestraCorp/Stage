---
title: P1b 身份管理、认证与授权实施入口
status: implementation-in-progress
implementation_status: assignments-login-mfa-server-implemented-client-integration-pending
review_date: 2026-10-26
policy_effect: none
---

# P1b 身份管理、认证与授权实施入口

本轮在联调前完成 Stage 身份/权限代码复查、输入边界修复、结果恢复与并行人员隔离回归，并建立 PostgreSQL 组织 guard 基线。详见[收尾记录](reviews/p1b-closeout-2026-10-25.md)和[逐项 I1 覆盖](reviews/p1b-identity-coverage-2026-10-25.md)。[Maestro 合同](maestro-stage-p1b-handoff.md)已经固定，实际桌面登录/回调/凭据保管和联调仍未验收，不能将 Stage 最小路径收尾写成整个 P1b 或 P1 已通过。P1b.4 已交付本人任职发现、组织统一 MFA 强度/新鲜度和显式配置，用户选定密码＋TOTP，隔离真实 IdP 与 Stage 链路已有证据，见[本轮记录](reviews/p1b4-assignments-mfa-2026-10-26.md)。随后[原生认证](native-authentication-design.md)已实现并验证服务端因素绑定/恢复与最小 OAuth/OIDC；Maestro 的真实因素交互、任职选择 UI、恢复交互仍待验收；正式入离职与人员/权限管理接口也未交付，开发 CLI 不计为产品管理能力。

产品按企业私有化部署实施，不建设 SaaS 多租户选择和管理。`tenant_*` 是当前兼容存储名；一个数据库只有一个组织授权根和环境，根不等同于法人公司，集团/子公司等组织单元以后按需建模，部门/项目使用 scope。身份自身读取 query 仅接受 `profile_ref/profile_version`，本人任职发现另外接受 `limit/after`，响应使用 `organization_ref`；旧未分版本路由移除。详见[组织授权根语义](organization-root-semantics.md)和[部署/API 边界](private-deployment-api-v1.md)。

P1a 提供组件运行、PG/Redis 资源、短事务命令与可靠投递基础。P1b 将真实认证、持久身份事实和当前授权接入同一执行链。本文整理实施顺序；首个公开身份接口已冻结为 `/api/v1/identity/me`，其他 IdP/profile 和客户端拓扑分别验收；详细要求继续以[身份规范](identity-and-authorization.md)和[一期方案](phase-1.md)为准。

2026-10-25 已确认[身份与授权表结构设计](identity-schema-design.md)。P1b.1 完成真实认证、组织成员读取与 bootstrap；P1b.2 增加单任职、固定角色版本、scope/resource/direct ACL 与内部数据库 loader。P1b.3 将短期人类认证证据与同一命令事务连接，以已有 WorkItem 标题/说明编辑验证业务结果、认证/授权审计和原 key 恢复，见[本轮人员命令记录](reviews/p1b3-human-work-2026-10-25.md)。

一期固定为**人员—公司—人员**模式：人员承担业务责任，Agent 是受监督的工具，当前 HUMAN loader 与人类任职约束保持。独立 Agent 成员、角色、业务接口、binding、Delegation 与通用 ServiceGrant 不进入当前切片；技术服务认证不授予人员协作权限。用户已明确恢复开发，并批准元数据编辑这一最小操作，范围见[人员协作边界](people-company-collaboration.md)。

## 1. 三层责任

| 层 | 回答的问题 | 交付 |
|---|---|---|
| 认证 Authentication | 这次请求的凭据由谁签发、代表哪个主体？ | 受信任 OIDC/OAuth profile、Stage access-token 验证、AuthenticationEvidence、主体映射 |
| 身份与授权事实 | 人员是否为本企业当前成员、选择哪个任职、与对象是什么关系？ | 人类 Principal、ExternalIdentity、OrganizationMembership、RoleAssignment、角色版本、scope/resource/关系及版本/撤销 |
| 授权与强制执行 Authorization | 当前主体对这个对象能执行什么？ | PostgreSQL 可信 loader、RBAC/ReBAC/ABAC、统一命令与读投影、提交前重验和审计 |

IdP 管登录凭据和会话；Stage 管组织事实与业务许可。Maestro 在本地负责人员登录交互和选择任职，云端 Stage 验证面向自己的 access token。部署组织由服务端唯一根记录决定，客户端不能选择或覆盖；任职选择须验证归属与当前状态。AI 辅助与手动操作使用同一业务 API、动作和对象规则，客户端不能以 Agent ID、用户声明或工具生成内容建立可信身份；工具辅助标记不等于人工确认。

## 2. 当前代码接缝

- `PrincipalTable`、认证来源、organization/membership 与 bootstrap 已落地。迁移 `f4c7b2a1e903` 新建六张授权表，并增量扩展旧任职/关系；未映射历史行保持可空且被新 loader 排除，不补默认组织或成员。
- `DatabaseAuthorizationLoader` 只支持 HUMAN、选定的单任职和已登记 WorkItem，权限来自固定角色版本 action 集合、摘要及可信策略；不按角色名称隐式授予。`viewer_of/owner_of` 是本切片支持的 direct ACL；任务分派/验收关系等待领域权威 loader。
- `AuthorizationEngine` 原有有限 Agent 判定结构保留为历史可信 fixture，不进入一期生产业务路径；数据库 loader 明确拒绝 service/agent，符合当前人员业务边界。独立 Agent/Delegation 无默认实施阶段；ServiceGrant 按实际技术后台任务另案，不能给 Worker 虚构人类任职，也不能将技术服务 token 解释成人员认证。
- 默认装配的 `HumanWorkService` 每次调用取得身份与存储 lease，认证后构造执行上下文。Core 管依赖存活，用户 token 和角色不进入 Core 状态。
- 公开人员命令使用同一个 `AuthenticatedAuthorizationLoader` 作为 loader、coordinator 和 `identity_validator`。协调顺序为 TrustState SHARE→Principal SHARE→组织 guard；内部数据库 loader 保留当前事务锁标记，认证证据在锁后及提交前重验。无真实认证的旧 fixture 路径不公开；IdP 网络调用不得占用数据库锁。
- 授权开发 CLI 由本机 OS 操作者在 dev/test 安装有限角色版本/任职/ACL，并撤销五类授权事实；幂等 journal、授权 epoch 和变更同事务。它不是 HTTP 管理后门或企业人员管理系统，使用范围见[开发操作说明](authorization-development.md)。
- `/api/v1/identity/me` 返回最小身份/成员投影；`GET /api/v1/identity/assignments` 返回当前人员/成员经历下的有效任职选择，按有界候选分页、不返回合并权限；三条 WorkItem 读、元数据写与原 key 结果读接口，始终使用真实人员凭据和当前单任职，API 与 OpenAPI 同步。没有公开创建工作项或授予权限端点；fixture 不作为协议失败兜底。

- `OrganizationHumanAuthenticationPolicyTable` 保存全组织 human 认证要求。启用后精确匹配 profile/version 和 ACR、要求 AMR 子集、按 auth_time 限制认证年龄；所有人员读取与命令共用强制入口，短期证据固定策略版本/摘要和期限。受控配置在组织 guard 内原子更新版本、epoch 与操作审计；详见[登录 MFA 操作](login-mfa-development.md)。

## 3. 实施顺序与退出依据

| 切片 | 交付 | 验证 |
|---|---|---|
| P1b.1 认证与成员首条切片 | Principal 扩展、trust/provider/profile/企业准入、外部映射、tenant/membership、受控 bootstrap、固定 access-token profile 与最小只读 API | 迁移与约束、真实签名凭据验证、issuer/namespace/sub 精确映射、来源准入、成员当前性、重复初始化不增权 |
| P1b.2 任职与资源授权 | 已实现 scope/resource、角色版本/任职、直接 ACL、数据库 loader、受控开发变更和审计；该轮范围为可信内部命令 | 单组织根与主体边界、固定成员经历/角色版本、scope/资源不匹配拒绝、禁止岗位并权、到期和撤销竞争；历史验证见该轮记录 |
| P1b.3 人类认证证据到命令重验 | 已接入短期真实认证证据和当前授权；开放已有 WorkItem 读取、标题/说明编辑、原 key 结果恢复；不增加表或迁移 | 原角色不自动增权；owner_of/draft/open/版本；profile/trust/映射/成员重验；认证到期回滚；PG 撤销竞争；真实 HTTP 与未知结果恢复分别留证 |
| P1b.4 本人任职与登录 MFA | 已实现受保护的本人任职分页、组织统一强度/新鲜度、版本化配置和统一 OpenAPI；已选密码＋TOTP，由真实隔离 IdP 验证 | 真实 IdP OTP 正反向、Stage HTTP/PG、策略变更与提交竞争分别留证；后续原生模块已补服务端因素绑定/恢复；Maestro 桌面交互和生产接入仍未验收；正式管理缺口单独记录 |

第一条身份读取切片只返回当前调用者及其部署企业中的成员投影，不提前返回尚无当前角色 loader 保证的任职能力。它用于证明认证→映射→企业来源准入→成员核验→最小投影贯通；真实签名凭据的单元/集成测试与成熟 IdP 授权码＋PKCE 联调分别留证，前者不替代后者。

迁移采用增量扩展，保留旧主键与历史记录。P1b.3 未增加表；P1b.4 增加 `organization_human_authentication_policy`，该轮为 26 张应用表；原生认证迁移 `e8a1c6d9f240` 再新增八张，后续多应用 OAuth 再增加三张表，当前 head `c6f3a19d820b`、37 张。已有组织迁移为 disabled/v1，新 bootstrap 无策略行等价 disabled/v0；必须显式配置后才强制 MFA。旧任职/关系归属不从 scope 字符串猜测；未映射行保持可空且被 loader 排除。人员 HTTP 由认证适配器构造可信上下文，身份读取仍只返回已建立的当前身份/成员事实，不承担权限票据职责。

## 4. 已固定的开发 profile 与客户端接缝

当前新增并启用[原生认证 profile](native-authentication-design.md) `stage-native-human@1`，本地密码＋TOTP、恢复与最小 OAuth/OIDC 已完成服务端验收，见[本轮记录](reviews/native-authentication-2026-10-26.md)。以下 Keycloak 描述保留首条切片的历史协议边界，不能作为当前默认人员登录前提。

1. **本地测试 IdP 与实际 token profile。** 首条切片采用隔离 Keycloak 26.4.7 + OpenJDK 21，固定 `stage.keycloak-access.v1`：RS256、header typ=JWT、payload typ=Bearer、Stage audience、准确 issuer/client 与互斥的 `stage_credential_kind`。它不是 RFC 9068 或任意 JWT 通用实现，不接入尚未使用的 opaque profile；部署和具体边界见[本地 IdP 说明](../deployment/identity/README.md)。
2. **Maestro 的首个登录拓扑。** Desktop 可用系统浏览器授权码＋PKCE，再调用 Stage；Web/BFF 的会话存储与回调需独立约定。先确认本地产品入口，Stage 不把未知 origin 或回调加入默认信任。
3. **受信任配置与初始化。** 测试 issuer/audience/client、算法/JWKS、初始虚构主体与成员由离线开发 bootstrap 显式登记；首条切片不创建任职或 service grant，首次登录不自动成为管理员。CLI 仅接受 dev/test 并记录本机 OS 操作者，数据库变更和操作结果同事务，未知结果按原 key 查询。

4. **登录 MFA 与业务批准分开。** 本轮按用户选择密码＋TOTP，由 IdP 验证真实因素；Stage 已实现组织统一强度和认证年龄策略，不能选择旧弱 profile 绕过。隔离协议与真实 Stage 链路已有证据，Maestro 浏览器交互与用户因素绑定/恢复分别验收。批准 ceremony 须额外绑定冻结意图，按 P1c/P1d 独立选择 WebAuthn 或 IdP step-up，不因登录成功而自动批准业务。

上述部署参数按实际启用路径冻结并写入实施记录；未启用 profile 不宣称兼容。登录第二因素已选 TOTP；生产 IdP、KMS 和具体业务批准方案不在本轮选定。OIDC/OAuth 使用成熟实现，Stage 自研的是组织身份与执行语义。

## 5. 阶段验收边界

按身份规格中当前人员协作范围验收适用 I1 项；I1-13/14 的独立 Agent/绑定/委托，以及 I1-15/22 的通用 ServiceGrant 和独立 Agent 队列子项明确后置，不作为 P1b 退出门槛，也不能计为通过。I1-15 的技术 Worker 用途限制、I1-22 中实际启用的人员排队动作当前重验仍需验收。已有 service 认证测试继续说明技术认证范围。未支持 profile 明确拒绝，未运行正向场景不能计为通过。登录 MFA 的 I1-06 仍在 P1b 验收；绑定意图的业务批准与签名/WASM 相关场景在 P1c/P1d 继续。Redis 失效或缓存命中均不能替代 PostgreSQL 当前授权；原 token 不写队列、审计或通用缓存。

后续若增加人员发起的队列动作，须保存原人员、成员经历、选定任职、动作/目标与原命令因果链，并在执行和提交时重新授权；实际确认记录与工具辅助来源分别保存。没有经过验证的延迟身份/恢复协议时，该执行方式保持关闭，不由 Worker 或 Agent 名称推导权限。

正式人员入离职、角色/任职/权限管理接口和个人资料 CRUD 当前仍未提供：后续按实际管理需求收敛最小合同与审计协议，不因 schema 或开发 CLI 存在而视为完成，也不要求先建设完整 IAM 管理平台。

完整任务业务、人类最终验收及职责分离按已有 P2 规格推进；P1b 要提供能够承载这些规则的可信主体、关系与授权输入。本地 Maestro 的 UI 与模型配置保持独立研发，通过版本化 API 合同对齐。
