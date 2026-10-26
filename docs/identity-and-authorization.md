---
title: Stage 身份、认证与授权契约
status: accepted-design
implementation_status: p1b4-assignments-login-mfa-server-implemented-client-and-full-p1-pending
policy_effect: none
decision_date: 2026-10-24
revised_date: 2026-10-26
source_checked_at: 2026-10-24
conformance_suite: stage-identity-v1-draft
---

# Stage 身份、认证与授权契约

产品约束：**一套 Stage 对应一个组织授权根、一套专属数据库、一个部署环境**。组织根由服务端单例记录决定，是信任、治理、授权 epoch 和审计边界，不等同于一个法人公司；集团、子公司、部门等业务组织单元以后按需建模。当前不提供公司/组织切换或 SaaS 租户管理。下文保留的 `tenant_*` 类型/字段表示兼容存储中的组织根；部门/项目边界用 scope 表达，scope 不自动等同组织树。当前身份 HTTP 合同为 `GET /api/v1/identity/me`，query 仅 `profile_ref/profile_version`，响应为 `organization_ref`；API 大版本、认证 profile 版本与行版本分别管理。详见[组织授权根语义](organization-root-semantics.md)和[私有化部署与 API v1](private-deployment-api-v1.md)。

本文定义企业私有化部署中的身份与授权基座，以及制品批准所需的身份依据。2026-10-25 已实现真实认证、当前单任职授权与已有 WorkItem 元数据编辑，证据见[Stage 侧收尾记录](reviews/p1b-closeout-2026-10-25.md)和[逐项 I1 覆盖](reviews/p1b-identity-coverage-2026-10-25.md)。[P1b.4](reviews/p1b4-assignments-mfa-2026-10-26.md)已实现本人任职发现和组织统一 MFA 策略，密码＋TOTP 由隔离真实 IdP 与 Stage 链路验证。原生模块随后补齐服务端因素绑定/恢复；整体 P1b 尚未完成：Maestro 桌面与生产接入尚待验收，正式人员/权限管理不能由开发 CLI 代替。制品批准与 WASM 在 P1c/P1d 独立验收；独立 Agent 授权、Delegation 与通用 ServiceGrant 已从一期要求移出。

**一期协作边界（2026-10-25 对齐）**：采用“人员—公司—人员”。公司作为共同规则、成员与协作事实的组织载体；人员通过当前成员和任职协作并承担业务责任，Agent 仅是人员监督下的工具，不独立入职、持有角色或成为协作者，不设 Agent 专用业务接口。既有 `human / agent / service` 枚举与服务认证保留，不由此扩大一期范围。独立 Agent 登记、承载绑定、Delegation 和通用 ServiceGrant 为后置候选，无阶段承诺。详见[人员—公司—人员协作边界](people-company-collaboration.md)。

本轮一期先建设组件、真实认证协议、当前授权、隔离 WASM 与签名准入，再做基座联调；完整任务业务闭环后置。当前身份链已通过 WorkItem 元数据命令验证；后续 `EvaluateQualification` 用于身份、WASM 与提交的共同验收，不自动创建真实企业成员、发放岗位或授予权限。具体阶段以[一期方案](phase-1.md)为准，产品边界见[愿景与运行时方向](vision-and-runtime-direction.md)及 [AGENT.md](../AGENT.md)。

## 1. 架构决定与当前实现边界

现行决定是复用成熟密码学和协议库，在 Stage 同进程实现[最小本地认证与 OAuth/OIDC 模块](native-authentication-design.md)。该模块负责登录凭据、认证会话、登录 MFA 和账户恢复；Stage 的 IdentityService 负责把认证结果映射到内部主体，并核验公司成员、当前任职、资源关系和业务政策。本轮用户已选密码＋TOTP，认证强度按具体 IdP/profile 映射，并由组织统一策略限制所有 human 入口；其他因素及生产恢复政策不自动列为已支持。业务批准若选择 Stage RP WebAuthn，属于独立的批准凭据与意图协议，不改变 IdP 的登录职责。外部 Keycloak adapter 保留迁移兼容，不再是本地人员登录的长期前提。

统一认证来源与授权语义，不要求把所有产品或协议强行放进一个 IdP realm，也不要求一期拆出 IAM 微服务。Maestro / Virtuoso 保持独立产品与客户端注册；Stage 是独立受保护 API。部署企业可在受控信任配置下接受多个 issuer，不能由请求临时指定一个发行方就被信任。

| 责任方 | 权威内容 | 不由它自动决定的内容 |
|---|---|---|
| IdP / 授权服务器 | 登录、凭据、会话、token 签发与自身撤销状态 | 组织根内当前岗位、对象读权、独立验收和本次业务批准 |
| Stage Identity | 主体映射、企业成员、人员任职及状态版本；既有技术服务认证 | 替用户保存 IdP 密码，或凭客户端传来的角色建立任职 |
| Stage Authorization | 基于当前可信事实计算能力与资源判定 | 直接产生业务副作用，或把一次允许判定变成永久通行证 |
| Command Kernel / 各领域 | 强制授权、版本与业务不变式；持久执行、审计和结果 | 通过角色名称或“管理员 UI”绕过统一授权入口 |
| Maestro / Virtuoso | 登录交互、上下文选择、受权 API、结果展示 | 自行断言企业管理员，或在客户端复制一套服务端授权规则 |
| Blueprint / Release | 不可变批准对象、批准证据、签名、准入与激活记录 | 用发布批准代替后续每次业务动作的当前授权 |

现有源码保留 `human / agent / service` 枚举和历史纯判定结构。P1b.1 已有真实人类/技术服务认证与当前组织成员读取；P1b.2 已有固定角色版本、scope/direct ACL 和 HUMAN loader；P1b.3 将真实人员认证证据接入最小 WorkItem 元数据命令、提交前重验与结果恢复，见[本轮记录](reviews/p1b3-human-work-2026-10-25.md)。技术服务 token 不能进入人员业务路径。独立 Agent/委托和通用 ServiceGrant 不属于一期门槛；正式组织 IAM 写接口、人工批准、生产身份和 WASM 各自验收，不能从枚举或内部测试推导已完成。

## 2. 主体、账号、组织根与岗位分别建模

### 2.1 主体与外部登录标识

`Principal` 是 Stage 信任域内的稳定主体。`ExternalIdentity` 负责关联外部认证标识；不把 IdP 的用户记录、某次 token、email 或显示名直接当作 Stage 主体。

| 对象 | 建议最小字段与约束 | 权威与用途 |
|---|---|---|
| `Principal` | `principal_ref`、`kind: human / agent / service`、`status`、`identity_epoch`、`version` | 既有枚举保留；一期业务责任主体为 human，service 识别技术执行来源，agent 不是一期独立协作者 |
| `ExternalIdentity` | `identity_ref`、`provider_ref`、精确 `issuer` / `subject`、`subject_kind`、`principal_ref`、关联依据、状态与版本 | 经过受信任 adapter 验证后建立；OIDC 身份以 `(issuer, subject)` 识别，[S1] |
| `TenantMembership` | `tenant_ref`（组织根兼容引用）、`membership_ref`、`principal_ref`、状态、有效期、版本；同根/principal 最多一条未终结经历 | 一次加入该部署组织根的经历；left/revoked/expired 后重入创建新 ref，旧任职不自动恢复；不自动赋予根下所有组织单元或资源的读写权 |
| `HumanIdentityLink` | 受限的人类主体关联、核验来源、可信等级、状态与版本 | 在确有依据时防止同一人换账号绕过独立批准；不是默认收集真实身份证明的许可 |
| `WorkloadRegistration` | `service_principal_ref`、凭据注册引用、允许 issuer / client / 环境、状态、版本 | 将后台服务认证绑定到明确服务；client credentials 不是人类登录，[S7] |

OIDC 只保证 `sub` 在其 issuer 内稳定且不重新分配；email、用户名与同名 `sub` 不能跨 issuer 合并。[S1] 因发行方迁移而建立新映射，必须有受权的关联与复核记录，不直接修改历史审计中的原始来源。

人类 OIDC 身份与 workload 注册使用明确的来源类别。服务 token 即使出现与某个人相同的 `iss / sub` 字符串，也不能复用人类身份查找结果；必须按已登记的 grant / client 语义验证并走 service 注册。无法可靠区分的 provider profile 不接入。

支持 pairwise subject 的 IdP 可能对不同客户端 / sector 给同一个人不同 `sub`。因此 Maestro、Virtuoso 和 Stage 不能靠比较客户端看到的 `sub` 判断是否同一人。[S1] 一期选定受信任 broker / 授权服务器的 Stage API 主体命名空间，adapter 明确 token profile 与映射规则；需要关联其他客户端账号时采用有证明的关联流程。两个 issuer 返回相同 email，即使都声称 `email_verified=true`，也不足以自动合并主体或转移企业权限。

同一主体在多个独立 Stage 实例中时，各实例的成员关系分别存在、分别撤销。跨实例关联属于客户端或受控集成边界，普通 Stage API 只返回本实例所需引用与投影，不提供跨实例全局人员画像、角色或 token 查询。

部署组织根通过 `TenantAuthenticationBinding` 明确接受本次 profile/version、环境及 client/assurance 条件。同一 Principal 关联多个登录账号不扩大根的认证来源范围；有效 membership 不能让未经本根接受的低保障来源进入。provider/subject_namespace 的主体类型、subject 来源与映射语义跨 profile 版本保持一致，变更语义须新 namespace 及受控关联。

独立审批的保证以已核验的主体关联范围为限。两枚 Passkey、两个账号或两个工具执行实例不等于两个自然人；无法确定高影响批准要求的身份独立性时，应拒绝满足该要求或转交受控核验，不能用账号数量冒充独立性证明。

### 2.2 岗位、任职、职责与能力

| 概念 | 定义 | 设计规则 |
|---|---|---|
| `PositionDefinition` | 组织根内批准的岗位 / 职责定义及来源版本 | 职责说明是政策依据；文本不直接执行成授权代码 |
| `RoleAssignment` | 某人员在某范围内的具体任职 | 固定组织根、membership、人员主体、role/revision、scope、有效期、状态与版本；是否允许替补有独立记录 |
| `RoleDefinition / RoleVersion` | 稳定角色定义与不可变能力版本 | 任职固定准确版本；发布新版本不自动迁移旧任职，版本撤销由当前 loader 重验 |
| `CapabilitySpec` | 版本化操作契约，如 `qualification.evaluate.v1` | 与[核心对象](core-object-contracts.md)同名；有输入输出、执行预算与所需权限，注册能力不等于给任何人授权 |
| `PermissionGrant` / policy | 在选定人员任职及当前条件下允许什么操作 | 使用稳定能力 / action 标识，业务 handler 不判断“经理”等名称；通用服务授权为后置候选 |
| `RelationTuple` | 主体与具体资源的权威关系 | 包含 tenant、关系、来源、有效期、撤销状态及版本；拥有部门岗位不推导所有资源关系 |

一次受控命令选择明确的任职。默认不自动并集一个人的全部岗位；跨岗位操作必须由具体政策明确规定组合方式、责任和冲突。容量与独立性仍按实际责任主体计算，不能切换任职复制一份个人容量或给自己验收。

内置 `RoleCode → Permission` 映射保留给旧可信 fixture 路径。新数据库 loader 使用固定角色版本的明确 action 集合、内容摘要和受支持策略版本；角色名称不隐式授予权限，路由和客户端不得增加角色名称分支。

关系只有一个事实权威：member_of 来自 membership，任务 assignee_of/reviewer_of 来自对应领域分派；`RelationTuple` 只允许直接维护明确分配给它的 ACL。delegated_by 保留为后置候选关系，不建立一期来源表或 loader。角色/action 表的新行本身不会生成代码能力；可信 loader 只接纳受支持 ActionPolicy 版本下的能力，平台部署组织约束、scope 边界、主体类型和当前撤销等硬约束不由企业自定义角色覆盖。

### 2.3 一期人员监督工具的执行边界

一期人员业务路径统一为：真实人员认证 → 当前组织成员 → 明确选择一个任职 → 共享动作/对象/命令 → 当前授权与业务约束 → 事务和审计。Agent 可以辅助准备内容、分析与操作建议；生成的 actor、role、allowed 或 approved 字段都不能成为身份或批准事实。Maestro/可信宿主保管凭据并调用 Stage，不把人员 token 交给模型、Agent 沙箱或 WASM。

监督不等于逐次点击，也不等于无限委托。允许自动化辅助执行哪些动作，由经过验证的人员操作上下文与明确的动作合同决定；没有可验证执行依据的跨会话/排队业务动作不能仅靠 user_ref 继续。审计分别记录责任人员、实际技术执行来源和确有证据的人工确认；动作要求确认时，必须验证实际确认记录，不能把工具生成结果或一般登录态写成“人已批准”。一期不建立通用委托协议来替代这些边界。

已实现的 workload 认证用于识别技术服务；已有 outbox Worker 发送已提交的投递意图，不因此成为组织协作者，也不获得任意业务权限。新增后台业务任务的授权与重验合同按真实需求单独评审，不伪造人类任职、不让内部 Worker 默认管理员，也不将 ServiceGrant 作为一期人员闭环前置条件。

### 2.4 独立 Agent 与服务授权（后置候选，非一期实施约束）

本节保存此前的备选方案便于追溯，**不纳入一期建表、接口、loader 或验收，不承诺 P2/P3 等阶段**。需要独立软件协作者或明确后台任务时重新评审，不能把人员监督工具自动解释为以下模型。

若未来采用独立 Agent，`AgentRegistration` 候选固定其登记经历、所属根、成员经历与责任方；`AgentWorkloadBinding` 另行核验承载来源。`Delegation` 候选至少绑定：`delegation_ref`、组织根、固定 Agent 登记经历、委托人、任职引用、逐条 action-target term、有效期、是否可转委托、预算引用、状态、撤销时间与版本。每个 term 只选精确 resource 或明确 scope 匹配规则；禁止将动作数组与资源数组组合成笛卡尔积。ServiceGrant 若采用也需配对结构。候选最小方案只允许直接、不可转委托的有限委托，多级链另行评审。

代理动作的有效权限必须同时满足：

```text
委托人当前有效任职及资源权限
∩ 当前有效委托范围
∩ Agent 与承载 workload 的执行限制
∩ 资源政策、认证要求及业务不变式
```

不得把 Agent 独有的资源关系与委托人的另一项岗位权限拼成原本双方都没有的许可。`actor_principal_ref`、`authenticated_workload_ref`、`effective_principal_ref`、`assignment_ref`、`delegation_ref` 和实际责任主体分别记录；服务端核验 Agent 与 workload 的绑定，不能接受客户端自报 Agent ID。

未来后台任务如需要独立 `ServiceGrant`，应明确组织根、action、资源范围、责任组织、有效期和环境。它不冒充某个人，也不伪造人类任职。是否需要新增 service 授权分支由具体任务决定，不再作为 P1 通用建设项。

若未来选择独立委托方案，代表人执行的 worker 才按该合同同时持有自己的 workload 认证和有效委托引用。队列只保存业务命令、主体 / 委托引用、原始因果链与幂等身份；不保存用户 access / refresh token，不把入队时的 `allowed=true` 当作执行凭据。

## 3. 登录协议与 API 凭据分开验证

首次主体与信任配置采用[一期受控初始化](phase-1.md)记录：issuer/audience、RP/origin、发布验证根与明确的初始管理主体由部署方带外核验后登记，不从首个登录、包自带公钥或客户端角色推导。BootstrapRecord记录环境、来源与授予并关闭入口，重复初始化不增加权限；未知信任或未经关联的主体默认无组织权限。

### 3.1 人类登录与产品客户端

Maestro / Virtuoso 的 Web、Desktop、CLI 分别登记符合部署方式的 OAuth client。人类交互采用授权码流程与 PKCE、受控重定向、`state` 和 OIDC `nonce` 校验；公共客户端不内置可被用户提取的共享 client secret。客户端与回调细节须按选定成熟库及部署 profile 验收，不由自研组件内核实现密码学。[S1][S6][S7]

OIDC ID token 由登录客户端按 issuer、client audience、签名、到期和 nonce 等规则验证，用来确认登录。调用 Stage API 使用授权服务器授予 Stage 资源的 access token；API 不接受产品 ID token、refresh token、WebAuthn assertion 或发布包签名充当 access token。[S1][S2][S5]

复用 SSO 不等于产品共享同一 token 存储。Web 部署若采用 BFF，用户浏览器持受控会话 cookie，BFF 保存 token；若采用直接调用 API 的部署，按相应公共客户端 profile 处理。P1 先固定一种可验证调用拓扑；其他拓扑未验收前不声明受支持。无论哪种，用户模型 API key、Virtuoso 私人会话和本地缓存均不因此进入 Stage。

### 3.2 明确 adapter profile，不假设所有 token 都是 JWT

OAuth access token 可以是不透明字符串，也可以有可验证结构。[S7] Stage 为每个受信任 provider 登记接受的凭据类型、验证协议、Stage audience、主体语义和认证强度映射；token 的外观或未验证的 `iss` 最多用于定位允许列表中的候选，不能用于动态建立信任或任意联网。

| Adapter profile | 必须验证的事实 | 失败语义 |
|---|---|---|
| JWT access token | 登记的 profile / token 类型、算法允许列表、可信 issuer 与 JWKS、签名、Stage audience、主体语义、`exp`，以及 profile 规定的 `nbf / iat / client_id / scope` 等 | 任何必要事实缺失或不符则不产生可用认证证据；不因 JWT 能解码而放行 |
| RFC 9068 JWT profile | 上述条件加该规范要求的 header / claims 与验证规则，包含 `typ=at+jwt` 的类型识别 | 只有声明符合该 profile 的 provider 才按此规则解释；不把 `at+jwt` 强加为所有厂商 token 的通用格式，[S2] |
| Opaque access token | 向预配置、经过认证的 introspection endpoint 查询；要求 `active=true`，并按 provider 合同取得 Stage audience、subject、到期 / 有效性与主体类型等必要信息 | RFC 7662 的不少字段可选；若实际响应不足以安全映射本 profile，拒绝，不猜值，[S3] |
| Workload credential | 独立 client / 凭据注册、允许环境、受众、有效期及所需发送者证明 | 将结果映射为 service 主体；不能仅凭字符串与某个人的 `sub` 相同而变成人 |
| Dev/test fixture | 显式环境门禁、虚构主体、同样的可信 loader 与命令路径 | prod 启动拒绝装配；fixture 不能提供预制允许结果或覆盖真实 adapter 的失败 |

同 issuer 的不同 token 用途采用互斥验证规则，避免 ID token、client assertion 和 access token 相互替代。[S5] `scope` 限制 API 的可用范围，但不替代 Stage 内部当前成员、选定任职和资源授权。

IdP 的 `auth_time`、`acr`、`amr` 可能不存在或采用不同约定。adapter 记录原始可信含义及映射 profile 版本；不把 `iat` 当作最近登录时间，不把未见认证强度记成 MFA，不按字符串大小比较 `acr`。某动作要求的新鲜认证或强度证据不足时返回需要提升认证或拒绝。[S1]

### 3.3 登录 MFA 与业务批准分别验收

登录 MFA 回答“本次身份认证达到什么强度”，属于一期身份/P1b，不依赖 WASM 或批准功能。P1b.4 已实现组织统一策略：按精确 profile/version 选择规则，ACR 精确匹配、要求 AMR 子集、按已验证 auth_time 限制认证年龄，覆盖 human 的自身读取、任职发现、工作项和原结果恢复。未列出的 profile 同样拒绝，不能通过弱 profile 绕过。证据固定策略 version/digest，期限不超过凭据、成员/绑定和认证年龄的最短有效期；锁后和提交前重验。JWT 有效或刷新成功不单独证明完成第二因素。

本轮已选密码＋TOTP，并验证隔离真实 IdP 的因素成功/失败、弱凭据、刷新及 Stage HTTP/PG 路径；详见[本轮记录](reviews/p1b4-assignments-mfa-2026-10-26.md)。不足时返回 401 / `MFA_REQUIRED` 和 `Bearer error="insufficient_user_authentication"` challenge；Maestro 从可信部署配置发起强认证。原生模块已提供 TOTP 手工绑定、一次性恢复码及重新绑定，真实桌面交互和生产恢复仍待验收。Stage 原生凭据表保存 Argon2id 密码哈希和加密 OTP 种子，密钥位于数据库外；策略由受控 dev/test CLI 显式版本化配置，迁移不会自动开启，见[操作说明](login-mfa-development.md)。

业务批准回答“此人员是否明确批准这个固定对象版本与摘要”，还须冻结意图、防重放、当前批准权及独立证据，按第 7 节在 P1c/P1d 验收。普通登录 MFA 不能直接变成业务批准，完成批准 ceremony 也不能倒推普通登录 MFA 已交付。

### 3.4 网络入口、代理和凭据保管

一期基准由 Stage 自行验证 API token。反向代理只转发流量与经过约束的网络信息；外部请求的 `X-User`、`X-Roles`、`X-Tenant-ID`、`Forwarded`、`X-Forwarded-*` 不能建立可信身份或自动决定 WebAuthn origin。

只接受明确登记的代理跳数 / 地址，边缘清除攻击者可注入的安全相关头，代理到 Stage 的通信有认证与完整性保护，后端不能绕过代理直接暴露。如果未来支持代理完成认证，另定义有 audience、短时效、防重放及可核验签发方的内部断言 profile，并测试直连与头注入；不是信任一个普通头即可。[S6]

JWKS、introspection 和元数据地址来自受控 provider 配置。禁止跟随 token 中任意 `jku / x5u` 或未登记 issuer 发起取钥请求；`kid` 是已登记 key set 内的选择提示，不是任意路径或 SQL 片段。[S5]

服务凭据优先采用部署可支持的短期凭据或非对称 client authentication；具体 mTLS / private-key JWT / 工作负载平台能力由 profile 决定。P1 可用隔离本地测试凭据验证合同，生产凭据不能进入仓库、UI 资产、模型上下文、普通日志或 WASM。[S6][S7]

## 4. 可复用数据契约与生命周期

下列字段描述目标逻辑 schema；当前人员认证证据与命令路径已实现其中的适用子集，批准/工作流等目标仍待实现。具体命名以已发布 API 和代码合同为准，后续扩展不能省略字段权威与生命周期。字段缺失必须显式为 unknown / null，不能填造值。

### 4.1 `AuthenticationEvidence`

由受信任 adapter 产生，描述一次入口凭据验证；不是角色清单或可供客户端改写的 DTO。

| 字段组 | 逻辑字段 | 来源与寿命 |
|---|---|---|
| 证据身份 | `evidence_ref`、`adapter_profile_ref/version`、`credential_kind` | Stage 生成关联引用；profile 来自安全配置 |
| 主体来源 | `external_identity_ref`、`issuer`、`subject`、`subject_kind`、已映射 `principal_ref/kind` | 签名 / introspection 结果与受控映射共同确定；未知主体不自动加入组织根 |
| 调用对象 | `audiences`、`client_ref`、`sender_binding_ref`、`token_scope` | 仅记录实际验证到的信息；未采用发送者约束时不能声称具备它 |
| 时间 | `validated_at`、`credential_expires_at`、`evidence_valid_until`、可空 `authenticated_at` | 有效截止取凭据、profile 新鲜度和必要在线检查的严格上界；不是延长原凭据 |
| 认证强度 | `assurance_profile_ref/version`、可空 `acr/amr`、`user_verification`、`recovery_epoch` | 只用可核验来源及已登记映射；普通 access token 不当然含 WebAuthn UV 证据 |
| 当前性引用 | `identity_epoch`、external identity/workload registration 版本、trust 配置/key revision、组织根 binding/version、`revocation_checked_at` | 记录本次实际查询到的版本和时间；没有即时撤销通道时明确窗口 |

原 token 仅在入口验证需要的最短期间使用。持久审计记录证据引用、经筛选的事实和验证 profile；需要诊断关联时用受限不可逆引用，不记录 bearer token 或 refresh token。完整 WebAuthn 批准断言另存受限证据库，不进入普通业务日志。

证据首先是请求内存对象，未创建持久证据行时 evidence_ref 只作关联引用。提交前同时重验映射/registration、主体、租户来源准入及有效截止；只检查 principal.active 不足以证明认证来源仍有效。租户写入的受限证据摘要与业务结果同事务保存；平台身份写入使用独立 `IdentityControlOperation` 的幂等结果/审计，不能虚构 system tenant。

### 4.2 `ExecutionContext` 与可信加载

| 字段组 | 逻辑字段 | 权威与边界 |
|---|---|---|
| 执行身份 | `execution_ref`、`request_ref`、`attempt_ref`、`causation_ref`、`deadline` | 服务端建立；重试 attempt 可变，逻辑命令及幂等身份保持稳定 |
| 认证引用 | `authentication_evidence_ref`、`authenticated_principal_ref`、可空 `authenticated_workload_ref` | 从 adapter 证据与受控运行器得出，不从 body 复制 |
| 业务主体 | `actor_principal_ref`、`effective_principal_ref`、`responsible_principal_ref` | 一期业务命令根据已验证的人员身份推导，不由 Agent 内容、service token 或自报 user_ref 代替 |
| 执行范围 | 服务端组织根（内部 `tenant_ref`）、当前 `membership_ref`、选定 `assignment_ref` | 组织根由服务端唯一记录决定；请求可选择任职，Stage 验证归属、有效性、范围与版本；兼容 delegation/grant 字段不纳入一期必填或公开协议 |
| 执行来源 | `command_ref/version`、可空 `workflow_run_ref`、`release_ref/digest`、`capability_ref/version`、provider generation | 由命令 / 运行实例绑定，供追踪和一致性验证；不授予业务权限 |

生命周期为一次受控命令 / 消息 attempt。`ComponentContext` 不保存共享“当前用户 / 当前组织”；`ContextVar` 可传 trace，不作为授权权威。进入后台队列、重试或新流程步骤时重新建立上下文。

`TrustedContextLoader` 在当前受控事务中读取人员成员、选定任职、关系、主体状态、资源归属、版本、策略与必要资格，生成 `AuthorizationContext`。认证证据说明谁进入系统，当前加载说明这次还能做什么。loader 只读取判定需要的最小权威事实，不应默认加载用户完整档案。

### 4.3 `AuthorizationContext` 与 `AuthorizationDecision`

现有 `AuthorizationContext` 的 subject / action / resource / organization / assignment / relations / attributes / policy / now 保留语义；一期补齐事实版本与认证要求，不能把它公开为客户端可提交的“完整可信上下文”。

`AuthorizationDecision` 建议保留现有 outcome、action、subject、resource、policy、evaluated_at、decision_id、request、assignment；既有 delegation 字段仅兼容保留，不要求一期填充。人员路径增加：组织根引用、effective principal、evidence 引用、授权 epoch、资源与事实版本摘要、必要义务 / 数据投影、稳定原因码及使用边界。

判定只解释指定动作、资源、输入和评估时点。`allowed` 既不预留容量，也不证明业务提交，不是下一请求可重用的 token。`needs_approval / expired / denied / unavailable` 要分别处理；既有 `needs_delegation` 属于后置候选路径，不引出一期 Agent 接口；未知政策、未知认证强度或缺必要事实不能默认允许。

允许判定与执行审计在业务事务中关联。查询、附件下载、搜索、导出、审计读取、SSE 和通知均须有相应的受权投影，不能仅保护写接口。客户端能力预览用于交互，不构成提交时的许可。

### 4.4 共用模块，不复制政策

P1 在 Stage 内以清晰模块组织 `authentication → identity loader → authz → command enforcement`。人员经 HTTP、CLI 或工具辅助使用同一受控查询与命令路径；Worker 只执行其已定义的技术任务，不以 Agent 名义创建独立协作通道。各领域负责强制检查自身入口和不变式。

Maestro / Virtuoso 共享版本化 API schema、生成 typed client 的来源、上下文选择协议、错误码和兼容测试。SDK 不内置公司角色规则，不返回可在另一个服务中脱离当前事实使用的 `allowed=true` 授权票据。

`AuthenticationAdapter`、key resolver、identity repository、授权引擎可作为受信任组件服务装配；provider 的替换遵循 Core generation / drain 规则。组件替换本身不继承或扩大用户授权；认证与强制授权服务不得由普通业务包覆盖。后续若拆分服务，须另定受众、传递凭据、撤销一致性与决策版本协议。

## 5. 一期 `EvaluateQualification` 如何贯穿身份边界

该命令只生成受权资格评估记录。WASM 返回的数据是有限规则的计算结果，不是 `AuthorizationDecision`，不能自行建立成员、任职、委托或批准发布。

```text
入口验证真实凭据，映射主体，加载唯一部署组织与当前成员
→ 固定可信 command handler 确定评估对象、规则包及输入 schema
→ 短事务：按一期锁序持guard与FactSet锁，一致读取当前权限、最小事实与revision/digest
→ 结束事务，交独立 WASM runner 做有预算的纯计算
→ 新短事务：锁授权 guard 与目标，重载当前身份 / 权限 / 规则准入
→ 重验凭据期限、deadline、事实 version / digest 和输出 schema
→ 固定 handler 判断结果可否接纳
→ 同事务保存评估结果、decision、审计和 outbox
```

读阶段的允许不能覆盖提交阶段。计算期间撤销成员 / 任职、规则包被撤销、输入事实变化或凭据到期，最终事务必须拒绝或返回可重试冲突；不得沿用旧 `allow`。WASM 执行期间不持数据库事务锁，也不携带用户 token、完整 `AuthenticationEvidence`、数据库句柄或可扩大权限的通用宿主调用。

若当前授权不再允许读取原计算结果，拒绝响应不回显敏感输入或结果；审计保留受限摘要与原因。一个可信业务 handler 可以依据当前规则使用评估结果，但不得把模块输出的“合格”解释成获得任意业务执行权。

## 6. 撤销、缓存、密钥与账户恢复

### 6.1 分开四种撤销

**旧 Keycloak adapter 的实现事实**：该 profile 离线验证 JWT，尚无 introspection/back-channel logout/账户禁用事件同步。真实 IdP 注销会使 refresh token 失效，但未到期 access token 仍可能被 Stage 接受，直到自身 exp 或 Stage 本地主体/成员/任职/profile 撤销。原生账号另外在业务持锁路径重验本地会话/凭据版本，退出和改密码与命令提交协调，即使 JWT 未过期也会拒绝。默认 60 秒 evidence TTL 只限制一份内部证据，不能写成远端撤销上界。具体实测与未覆盖项见[覆盖矩阵](reviews/p1b-identity-coverage-2026-10-25.md)和[收尾记录](reviews/p1b-closeout-2026-10-25.md)；下表包含目标协议，不能将其全部解释为当前已实现。

| 变更 | 即时权威与处理 | 不能假称的保证 |
|---|---|---|
| token / IdP session 撤销 | provider 的撤销、introspection 或已验收事件协议；本地凭据阻断记录 | 仅验证自包含 JWT 签名，不能得知 IdP 刚刚撤销了该 token，[S3][S4] |
| Stage 主体 / 成员 / 任职撤销 | 当前 Stage 记录、版本与授权 guard；新受控动作重读 | 不等 token 到期才失效，也不倒改撤销之前已提交的业务事实 |
| IdP / workload key 撤销 | 受控 key 状态、可信 key set 与紧急阻断 | `kid` 更换不等于账户撤销；从 JWKS 移除 key 也不是自动撤销全平台会话的协议 |
| 发布密钥 / 包 / 批准撤销 | 发布信任配置、批准状态、包状态与准入版本 | 历史签名仍可密码学验证，不表示现在允许激活或继续业务执行 |

普通 key rotation 支持新旧 key 的明确重叠期：可信 JWKS 缓存有界，未知 `kid` 至多触发受控刷新和限流，仍未知则拒绝。算法与用途由配置限定，不由 token 指定。发生疑似泄露时通过 key / issuer 阻断与新配置版本使旧缓存失效；不能仅等缓存自然过期。[S1][S5]

验证网络不可用时，不为未知 key、过期元数据或缺必要在线状态降级放行。已验证的缓存是否仍可使用必须有明确 profile 与最长时效；超出窗口返回不可用。introspection 的成功缓存不会超过 `exp`，同时承认缓存期间的撤销延迟；要求即时性的动作不用跨请求成功缓存。[S3]

远端取钥 / introspection 在业务事务之外完成；锁内重验本地身份状态、证据时效与其记录的撤销版本，不持数据库锁等待 IdP 网络响应。即使禁用跨请求缓存，远端查询完成到本地提交之间仍有窗口；若某动作要求跨系统即时撤销，需另定在线授权协议或等价机制，不能从本地 guard 推导出跨 IdP 的原子性。

### 6.2 当前授权缓存与并发

沿用一期部署组织 `AuthorizationGuard` 协议：组织范围的成员、任职、关系和有效策略写者持组织X并推进其epoch；普通受控业务持组织S，主体和信任变更分别由principal/trust保护。只有冲突操作互相等待，普通读者之间可并行。资源版本、幂等和领域锁仍保留。初始授权在取得必要锁后，最终授权时效与身份复核在提交前再次进行；命令deadline和当前凭据期限共同检查。具体实现见[并发优化记录](reviews/concurrency-optimization-2026-10-26.md)。

写命令不复用跨命令的正向授权结果。若查询引入缓存，key 至少包含组织根、人员主体、选定任职、action、资源 / 投影、策略版本与 epoch，另有过期与失效机制；缓存命中不免除入口认证。负向缓存也要有界，避免新授权长期无法生效。

主体禁用、账号重新关联或平台策略变更可能影响多项任职与并发请求。按[统一锁序](phase-1.md#transaction-protocol)，身份读取与 P1b.3 人员命令持相关 trust/principal `FOR SHARE`，对应身份/信任撤销持 `FOR UPDATE`，随后取得组织 guard。认证包装 loader 将 profile/trust/外部映射/来源准入与固定成员、单任职及当前资源事实联合重验，提交前再次检查有效期。已有身份、授权与新命令竞争测试分别留证；独立服务/Agent 委托仍是后置候选。新增路径须覆盖完整依赖和共同锁点，无法证明时拒绝相关变更。

排队动作、长期人工等待和恢复后的步骤都重新判定当前权限。历史批准是一项事实，执行时是否继续认可它由明确发布 / 业务政策决定；离职人员旧批准的法律效力不由本技术规格推断。对正在进行的外部 I/O，本地撤销只能记录取消意图与实际结果，不能宣称原子收回已经发出的请求。

### 6.3 账户恢复与身份关联

IdP 恢复登录能力不自动恢复 Stage 已撤销的任职或发布签名能力。涉及恢复事件的认证证据标记可核验的 recovery 状态 / epoch；高影响批准按政策要求重新确认有效凭据和新鲜认证。

新增 / 替换 Stage Passkey、合并主体、迁移 issuer、转移 workload 所有权均走受控命令与审计。已有任职不随 email 修改转移到另一个主体；账号重建后获得新 `sub`，也不自动继承旧主体。

遗失全部凭据时走独立恢复政策：核验谁可恢复、如何通知与撤销旧凭据、怎样解除临时限制。P1 验证真实协议登录以及虚构账户的恢复 / 撤销边界，不交付真实自然人实人核验或默认安全问题兜底。应急账号如以后引入，必须独立、限时且审计，不能被业务蓝图创建或启用。

## 7. 业务批准、组织签名与执行准入

### 7.1 两类签名的职责

WebAuthn 基础认证断言签署的是 authenticator data 与 client data hash 的组合；challenge 在 client data 中被绑定。它不是“调用 Passkey 私钥直接签任意 WASM 文件”的通用接口。[S8] 普通 2FA 登录成功也不等于用户签署了某个业务变更。

本文固定“人员对具体意图形成批准证据，组织发布密钥签发布清单”的职责分离。批准 ceremony 尚待选择 Stage RP WebAuthn 或明确绑定意图的 IdP step-up profile；下文保留两种候选的约束，不据此宣称已经选定 Passkey。登录 MFA 与此项独立，两类证据、密钥用途和验证链分别管理：

| 环节 | 要证明的内容 | 结果 |
|---|---|---|
| 人员认证 | 当前进入系统的是哪个受信任主体 | `AuthenticationEvidence` |
| 特定发布批准 | 此主体以有效批准权限确认了这个不变意图 | `ApprovalEvidence`；不直接授予运行时业务权限 |
| 组织签名 | 发布服务依据有效批准政策认可这份完整清单 | `ReleaseEnvelope`，由组织发布 key 签署 |
| 激活 / 运行准入 | 该包目前可在此 tenant / 环境使用，能力与依赖符合约束 | `AdmissionDecision / ActivationReceipt` |
| 每次业务执行 | 当前人员 actor、成员/任职及资源事实允许具体动作 | 当前 `AuthorizationDecision` 与命令结果 |

P1 用隔离开发签名 key 验证协议，key 由 runner 外的受控签名方持有。生产使用 KMS / HSM 或同等可审计密钥设施的具体 provider 待选；不因此推迟发布清单、key 用途隔离及准入验收，也不把开发私钥当生产方案。

### 7.2 不可变批准意图和一次性 challenge

`ApprovalIntent` 至少冻结以下内容，并使用版本化规范编码求摘要：

- `intent_ref / schema_version`、tenant、环境、明确批准动作、申请人及来源；
- 发布清单 digest，覆盖 DSL / 规则 / WASM digest、依赖锁、宿主接口、能力上限、预算和配置绑定；秘密本身不写入清单，秘密使用受控引用与绑定版本；
- 目标发布序号、期望前置活动版本、批准政策版本、风险 / 影响摘要引用；
- 提交与批准截止时间、适用范围、所需独立批准者及允许的认证 profile。

选择的 digest 算法和编码版本由发布规格固定；不得对任意语言中未经规范化的 JSON / YAML 字符串默算摘要。可读语义 diff 与被批准清单必须引用同一不可变对象。

若选择直接 WebAuthn，须使用 Stage 自己登记的 RP credential，并执行以下验收；这是候选方案，尚非当前默认实现。IdP 域名下的 Passkey 不会因用户登录 Stage 就变成 Stage RP 的凭据；两个方案需分别配置和验收。

1. 管理员请求批准某个冻结 intent；服务端先确认身份、tenant、选定任职与当前批准能力，显示这份 intent 对应的摘要和差异。
2. 服务端生成至少 32 字节安全随机 nonce，生成 challenge，例如 `nonce || SHA-256(domain_separator || intent_digest)`；这是 Stage 协议选择，不是 W3C 规定的业务编码。challenge 记录绑定 intent、批准主体、会话 / ceremony、用途、过期时间与状态。
3. RP 发起 `webauthn.get`，要求 `userVerification=required`；批准 credential 必须已经通过受控注册绑定该主体。不能只信前端声称“已验证指纹”。
4. 成熟 verifier 检查签名、预期 challenge、`type`、精确允许 origin、RP ID hash、credential 归属、UP / UV 与所用扩展规则。P1 不接受跨 origin iframe 的批准 ceremony；代理头不能改变允许 origin。[S8]
5. 在短事务中按[一期统一锁序](phase-1.md#transaction-protocol)取得相关TrustState/主体行、tenant guard，再锁intent/challenge/批准记录；重验凭据状态、新鲜度、信任revision、批准能力、时间、政策和意图一致性，一次性消费challenge并写证据。并发重复提交不能形成第二份批准。
6. 超时、重放、修改环境 / digest / 动作、凭据撤销或任职失效均拒绝；意图改变必须新建 intent 并重新批准。已记录结果的响应丢失可受权查询，但不重新消耗 challenge 或增加批准票。

WebAuthn `signCount` 是克隆风险信号，部分认证器可以恒为零；它不能代替一次性 challenge。[S8] 若允许同步 Passkey，不能宣称凭据天然不可复制或严格绑定某一硬件；设备 / attestation 限制须成为单独的明确政策与验收。

普通 WebAuthn 用户验证不保证认证器屏幕可信展示了完整业务变更。冻结对象、页面内容与摘要一致、防止跨站请求和 UI 注入、可靠的 origin 验证仍有必要。批准记录只能声称已验证的事实，不能因指纹弹窗而声称管理员已阅读或理解全部变更。

若选择 IdP step-up，应将新的认证事务通过服务端保存的 `state / nonce / intent_ref` 绑定到原不变意图，验证 callback、所需 `auth_time / acr / amr` 和当前批准权后写证据。它证明受绑定的提升认证与业务批准流程，不等同于 Stage 持有可独立验证的“用户对制品的裸签名”；不得把这两种证据格式混用。[S1]

### 7.3 多人批准、签名与不可自改的信任根

多人批准是业务政策，例如要求两个具备指定能力且互相独立的人批准同一 intent。阈值密码签名则是多个密钥份额共同产生一个签名的密码协议。二者可以组合，但不能把保存两条批准记录称作已实现阈值签名。

P1采用固定单批准人政策，检验有权主体、意图绑定及重复证据不增加批准次数；双人正向流程留后续治理验收，不实现阈值密码协议或通用审批UI。同一人换账号、任职、工具执行实例或Passkey不应在未来多方政策中变成独立主体；申请、批准、发布、激活权限分别定义，待批准包不能把规则改低。

发布服务在签名前重新确认 intent 未变化、批准政策版本仍可用、所需批准未撤销并且符合当前政策。原始断言、credential 公钥 / key 标识、intent digest、批准主体及任职、验证配置版本和时间作为受限证据保存。变更签名内容或追加未批准权限必须走新 intent。

对外签名调用也有独立请求身份与结果记录：批准决策、签名返回和激活不能假装是一个跨 KMS / 数据库的原子事务。响应未知时先按原签名请求对账；已产生的有效签名仍须通过当前准入，签名期间发生的撤销不得被一次早先允许结果覆盖。

IdP trust、认证 adapter 配置、组织根 key、验签开关、可签用途、批准规则、身份关联与授权强制入口属于平台安全配置。普通业务蓝图、WASM 和业务组件配置不能自行替换它们。可信认证或规则实现可以通过受控组件装配提供，业务规则也可以模块化；实现可替换不意味着业务模块有权修改自己的信任来源或绕过授权强制入口。key 轮换、根信任变更及恢复由现行可信规则批准并保留明确过渡；新 key 不能仅凭自己签一份声明就成为信任根。

业务发布序号不因回滚而降低；回滚引用旧内容 digest，但形成新的受权发布动作。历史合法签名不绕过当前包撤销、环境匹配或防降级检查。停止接受新实例、在途步骤怎样处理和是否需人工接管应由发布规格区分，不能只定义一个含糊的“撤销签名”。

## 8. 隐私、最小输入与可追责

“重新加载当前身份”指获取当前判定必需且可信的状态，不是每步获取完整个人资料。普通资格评估可只需本组织根的人员主体引用、有效成员、选定任职、某项资格证明和版本；不必取得私人会话、全部任职、出生信息或无关成员关系。

对外投影采用 tenant 范围内的必要标识；用于防自批的内部主体关联由受限服务比较，向一般审批 UI 可只返回“独立性满足 / 不满足 / 无法确认”及受权原因。审计既要能定位责任，也要有读者范围和数据保留政策，不默认公开全局身份映射。

WASM 输入采用 capability schema 规定的最小投影，宿主在输入前授权、输出后校验与最终重验。P1 纯计算模块不能拿到用户 token、刷新凭据、Passkey 私钥、组织签名 key、完整组织目录或数据库连接。后续宿主能力接口也只接受受限句柄和类型化请求，由宿主重新授权，不能把请求中的 tenant / actor 当事实。

后续若引入可验证凭证或选择性披露，可信 loader 可验证“此主体在该挑战下具备资格”等最小证明及其当前性。不得在验证最小证明之后又无条件取回完整身份档案；同样不能用匿名证明掩盖本动作明确要求的责任或独立批准条件。证明绑定、发行方、撤销、重复使用和不可关联性需另立协议，本文件不声称已经实现。

## 9. P1 交付与验收

### 9.1 范围与退出条件

| 阶段 | 本文承担的交付 | 退出门槛 |
|---|---|---|
| P1a 共同契约 | 人员主体、外部身份、组织根成员、选定任职；三类上下文与 decision schema；组件与身份 provider 边界 | 语义、来源、失效及与 Command Kernel 的使用约束通过评审 |
| P1b 真实协议与当前授权 | 至少一个成熟本地测试 IdP；真实授权码 / API token 验证；登录 MFA 的 profile 与实际链路；本人任职发现与选择；当前 loader、部署根/scope/主体边界、撤销及 fixture 门禁 | 当前支持 profile 的正反向证据、客户端及登录 MFA 分别验收；开发 CLI 不计为正式人员/权限管理；不能仅用 fixture 代替 |
| P1c WASM / 发布接口 | workload 到 runner 的身份边界；批准意图、开发签名 key 分离及准入所需身份 / 审批证据接口 | token 不进入 WASM；有限发布对象通过有效批准与签名准入；不建设通用蓝图编辑器 |
| P1d 基座联调 | 真实 token → tenant scope → `EvaluateQualification` → 纯计算 → 当前重验 → 结果 / 审计 / outbox；所选 profile 的最小业务批准 ceremony | 正负场景分别留存证据；虚构主体及开发 key；生产 IAM 验收单独记录 |

JWT / opaque 两类合同均定义清楚，P1 至少选择一个实际支持的 profile 完成真实协议链路。未选 profile 必须明确拒绝，不能宣称支持；相应场景记录为不在本次支持范围，不能计通过。以后增加该 profile 时，须执行对应真实网络协议测试；受控测试服务器与真实 IdP 的覆盖来源分别标注，不能把通用 mock 的 `active=true` 称作某个 IdP 已支持。支持列表只列实际验证的 provider / profile 组合。

若选择直接 WebAuthn，P1 可使用浏览器虚拟认证器验证 RP 注册、challenge 绑定与断言拒绝；这不证明真实硬件、跨设备同步、企业 attestation 或账号恢复体验已经验收。测试 IdP、浏览器配置和开发 key 只存在隔离测试环境；本文不安装依赖、不配置真实 IdP、不创建生产账号。

### 9.2 `I1` 验收场景

以下是 2026-10-24 建立的 **36 个目标场景**，建档时已执行 0、已通过 0。2026-10-25 按人员协作决策保留所有原 ID，但将独立 Agent/委托/通用服务授权场景标为 deferred；它们不再属于一期完整性要求。2026-10-25 首条切片的局部用例、PG17 竞争和真实 IdP/API 验证另记[实施证据](reviews/p1b-first-slice-2026-10-25.md)，不据局部通过将某个包含多项义务的 I1 场景整体标为完成。后续按原 ID 逐项补正反向证据、运行 profile、数据库/浏览器/IdP 版本及未覆盖范围；不与 Runtime Core 的数量相加冒充通过数。

| ID | 场景与正向预期 | 反向 / 并发预期 |
|---|---|---|
| I1-01 | 本地测试 IdP 完成授权码 + PKCE 登录，正确 state / nonce 回调建立会话 | 错误 state、nonce、重定向或复用 code 拒绝；无 fixture 旁路 |
| I1-02 | 符合已登记 profile 的 JWT access token 可访问 Stage | 错 issuer / audience、过期、nbf 未到、未允许算法、坏签名拒绝 |
| I1-03 | 已登记 opaque token 经实际 introspection 得到足够可信事实 | inactive、到期、缺必要 audience / subject、端点失败不默认放行 |
| I1-04 | 登录客户端验证自己的 ID token；API 使用独立 access token | 产品 ID token、refresh token、client assertion、发布签名不能替代 API token |
| I1-05 | JWKS 正常轮换在明确重叠期接受合法新旧 key | 未知 kid 刷新有界；恶意 jku / x5u 不访问；被禁 key 与过期缓存拒绝 |
| I1-06 | profile 明确解释 auth_time / acr / amr；真实 IdP＋客户端完成所选登录 MFA，Stage 核验强度与适用新鲜度 | 仅单因素、取消/失败、缺失或过期强度证据不能满足 MFA；刷新/恢复不抬高强度；iat 不代替 auth_time |
| I1-07 | 已登记代理链转发后能校验固定 origin 和原始请求边界 | 伪造身份 / forwarded 头、直连绕过、额外代理跳不建立可信身份 |
| I1-08 | 同一已核验 issuer/sub 稳定映射一个主体 | 同 email 或同 sub 不同 issuer 不合并；未验证账号关联不迁移权限 |
| I1-09 | pairwise 标识通过受控 broker / 明确关联映射到相应主体 | 不同 client 的 sub 不盲并；伪造关联或未证明同人不满足独立性要求 |
| I1-10 | 一个部署组织下部门/项目 scope 与各主体成员关系明确 | 第二组织根被拒绝；成员主体错配或未授权 scope/资源拒绝；唯一组织不等于全员有权 |
| I1-11 | 同一人选择明确任职执行该范围能力 | 客户端自报 role、切换岗位自动并权、岗位过期或越 scope 拒绝 |
| I1-12 | 当前资源关系与任职能力共同满足才允许 | 仅有公司成员身份、旧关系或别人的资源关系不能获得权限 |
| I1-13 | **deferred，非一期门槛**：若未来引入独立 Agent，再验证有限委托 | 改 Agent、委托人、任职、资源、动作、预算或到期范围拒绝；不可转委托 |
| I1-14 | **deferred，非一期门槛**：独立 Agent 与 workload 运行绑定；既有服务认证继续保留 | 单独提交 Agent ID、拼接无关关系、假冒人类 sub 或冒用 client_id 拒绝 |
| I1-15 | **一期适用**：已支持的技术 Worker 仅执行明确用途；**deferred 子项**：通用独立 ServiceGrant | Worker 不获得隐式管理员，不用虚构人类任职冒充业务权限；完整服务授权按真实需求评审 |
| I1-16 | 人员认证与已支持 service 认证产生来源明确且不可变的证据；Agent 不独立认证 | body 覆盖 evidence / tenant / actor / assurance 失败；原 token 不持久写日志 |
| I1-17 | 并行请求各自构造 tenant、主体、Session 与 provider 租约 | 组件全局变量、ContextVar 或前一请求上下文不能泄漏权限到后一请求 |
| I1-18 | 人员经 HTTP、CLI 或工具辅助走同一业务合同；技术 Worker 遵守自己的受控任务合同 | SDK allow 缓存、内部直调 handler、未核验消息身份不得绕过强制入口 |
| I1-19 | 查询 / 导出 / 附件 / 审计 / 通知返回允许投影 | 通过搜索、SSE、旧幂等结果或错误详情读取无权数据被阻断 |
| I1-20 | 最终事务记录当前 decision、事实版本、执行结果和审计 | 旧 decision ID 或模型 / WASM 输出不能作为下一次授权 token |
| I1-21 | 命令先持 guard 并提交，审计顺序清楚 | 撤销先提交时随后命令必须拒绝；两个数据库连接覆盖真实锁竞争 |
| I1-22 | **一期适用**：人员发起的排队业务动作如启用，执行前重建当前人员/成员/任职依据；**deferred 子项**：独立 Agent 委托队列 | 不能仅信 user_ref 或入队 allow；离职、撤销、时效终结须阻断新业务提交，不保存用户 token；已提交 outbox 的投递/取消按独立合同，不倒改原提交 |
| I1-23 | 缓存命中仍受 epoch、profile 时效与 token exp 限制 | 更换政策 / key / 成员状态后旧缓存不越权；网络失败不延长证据有效期 |
| I1-24 | 主体变更与企业内多主体/任职请求按共同锁序协调 | 绕过信任/主体锁或无法确定依赖集合时拒绝禁用 / 关联变更的提交 |
| I1-25 | IdP / Stage 凭据恢复后仍需当前任职与适用认证强度 | 恢复登录不复活已撤销岗位或旧成员经历；email 转移不转移历史主体 |
| I1-26 | Stage RP 注册凭据绑定已认证主体并验证注册 ceremony | 攻击者替换 credential / 主体、origin 或 RP ID 拒绝；IdP RP 凭据不默认复用 |
| I1-27 | Passkey 对冻结 intent 的一次性 challenge 产生有效批准证据 | digest、tenant、环境、动作、前置版本变化或 intent 过期必须重批 |
| I1-28 | 正确断言满足签名、challenge、type、origin、RP ID、UP / UV | 普通登录 MFA 标记、前端 UV 标记、坏签名、错来源不能批准 |
| I1-29 | 同一 challenge 成功消费一次，响应丢失可查询原结果 | 并发重放仅一条批准；signCount 为零不取消 nonce 防重放机制 |
| I1-30 | 固定批准政策被正确执行，P1采用一个有权批准人 | 重复证据不增加次数，不能降低政策或让无权主体批准；双人正向留后续，不计P1通过 |
| I1-31 | 独立发布服务复核批准后用 runner 外开发 key 签清单 | WASM、普通 handler 或凭单一登录态不能直接调用无限制签名接口 |
| I1-32 | 可信配置明确轮换 key、发布撤销及受权回滚 | 业务包自换根 key / 关闭验签 / 降低自己的批准要求 / 重放旧发布序号拒绝 |
| I1-33 | 资格评估只向 WASM 提供受权最小投影并接纳符合 schema 的结果 | token、完整身份、未授权 scope/主体事实不能进入 guest；guest 声称 allow 不授予业务权 |
| I1-34 | 纯计算后当前事实与权限未变，原子保存评估和审计 / outbox | 计算间撤销、事实版本变化、包撤销或认证过期，提交拒绝 / 冲突且不泄露旧结果 |
| I1-35 | fixture 显式启用时仍走真实 loader / policy，协议集成单独记录 | prod 装配 fixture 拒绝；fixture 通过不能代替 IdP / Passkey 协议通过 |
| I1-36 | 只加载判定必要状态与资格证明，受限审计可定位责任 | 不取私人会话、全局公司关系或无关档案；最小披露不被全量重载抵消 |

阶段分组：P1b验收I1-01～25、I1-35～36中的现行适用项，**排除 I1-13、I1-14 及 I1-15、I1-22 中明确后置的子项**；I1-15 的技术 Worker 用途限制与 I1-22 的人员排队当前重验仍按实际路径验收。后置内容保留原 ID，不计未完成一期门槛，也不计通过；P1c/P1d补I1-26～34并执行全链集成。I1-02与03按实际支持token profile记录，未选类型必须验证拒绝且不可记为正向通过；JWKS等profile特定子项同样记录适用性。I1-06 的登录 MFA 留在 P1b；本轮真实 IdP 与 Stage 服务端路径证据单独记录，不将它们等同于 Maestro 桌面及因素绑定/恢复验收。I1-26～29 描述直接 Stage RP WebAuthn 候选；若选择 IdP step-up，须另列等价批准证据用例并明确这些 RP 专属场景不适用，不能直接声称通过该组。批准 profile 的选择不得推迟或替代登录 MFA 验收。

## 10. 待决项与生产门槛

这些选择需要在相应实现前固定，不以“后续再说”绕过验收；它们不阻塞当前设计与隔离基座开发。

| 待决项 | P1 必须固定 | 正式部署前另验收 |
|---|---|---|
| IdP / 客户端拓扑 | 本地测试 IdP、支持的 JWT / opaque profile、Stage audience、回调、client 类型 | 每个生产 issuer 的合同、可用性、账户生命周期与恢复责任 |
| 主体映射 | broker 命名空间、pairwise 处理、关联状态和虚构同人映射 | 跨 IdP 人员关联与独立批准所需的真实核验强度 |
| 认证新鲜度 / 撤销 | 各证据时效上限、缓存、未知状态失败语义、测试撤销窗口 | 生产撤销事件 / introspection SLA、受影响会话和在途动作政策 |
| 登录 MFA | P1b 固定所选因素、IdP 强度映射、适用入口/新鲜度、刷新和恢复规则；实际端到端验证 | 生产因素登记/恢复责任、客户端兼容及紧急恢复；本轮已选密码＋TOTP |
| 业务批准 | P1c/P1d 前选定 Stage RP WebAuthn 或绑定意图的 IdP step-up profile；不默认选择某一种 | 真实 origin、适用凭据政策、恢复、硬件 / 移动端及可访问性 |
| 发布签名 | 版本化清单、开发 key 隔离、批准与 key 用途、撤销 / 防降级 | KMS / HSM provider、生产密钥恢复、根信任轮换与运行职责 |
| 人工责任与隐私 | 虚构批准政策、最小输入 schema、证据读者范围 | 公司正式批准政策、资料保留 / 删除、跨境与外部目录同步安排 |

尚未选择或测试的 provider、浏览器、密码库、发送者约束、阈值签名、隐私证明和多节点撤销协议不列为已支持能力。生产部署必须在实际 IdP 与入口拓扑完成独立协议和运维验收，不以本地测试 IdP 或当前设计文档替代。

## 11. 官方依据与核对范围

以下均于 **2026-10-24** 实际读取官方正文。标准规定与本文 Stage 方案有意分开：`AuthenticationEvidence` 字段、主体模型、guard、批准清单与 I1 场景均是 Stage 自己的契约；已实现范围以逐项证据为准，不是这些标准授予的认证。引用用于支持列明的精确范围，不宣称某个库或 IdP 已完整实现标准。

| 编号 | 一手来源 / 版本 | 实际核对范围与对本设计的限制 |
|---|---|---|
| S1 | [OpenID Connect Core 1.0](https://openid.net/specs/openid-connect-core-1_0.html) | §2、§3.1.3.7 ID token 字段与验证；§5.7 issuer/sub 的稳定标识；§8 pairwise；§10.1.1 签名 key 轮换。OIDC 不直接定义 Stage 公司任职和业务批准 |
| S2 | [RFC 9068 — JWT Profile for OAuth 2.0 Access Tokens](https://www.rfc-editor.org/rfc/rfc9068.html)，2021-10 | §2 token 类型 / claims、§4 验证、§5 混淆风险；仅适用于明确符合该 profile 的 token |
| S3 | [RFC 7662 — OAuth 2.0 Token Introspection](https://www.rfc-editor.org/rfc/rfc7662.html)，2015-10 | §2.1 / §2.2 请求、active 与可选元数据，§4 缓存 / 到期 / 撤销权衡；不承诺所有 provider 都返回 Stage 所需字段 |
| S4 | [RFC 7009 — OAuth 2.0 Token Revocation](https://www.rfc-editor.org/rfc/rfc7009.html)，2013-08 | §2.1 / §2.2 撤销与传播，§3 实现考虑；撤销端点存在不证明仅离线验签的资源服务器立即知晓撤销 |
| S5 | [RFC 8725 — JSON Web Token Best Current Practices](https://www.rfc-editor.org/rfc/rfc8725.html)，2020-02 | §2.8、§3.1、§3.8–§3.12：用途混淆、算法、issuer / audience、不可信取钥位置、显式类型与互斥验证规则 |
| S6 | [RFC 9700 — Best Current Practice for OAuth 2.0 Security](https://www.rfc-editor.org/rfc/rfc9700.html)，2025-01 | §2.1 授权码 / PKCE，§2.3 受众与权限范围，§2.5 client authentication，§4.13 TLS 终止代理边界；未据此宣称本项目已经实现 sender-constrained token |
| S7 | [RFC 6749 — The OAuth 2.0 Authorization Framework](https://www.rfc-editor.org/rfc/rfc6749.html)，2012-10 | §1.4 / §1.5 access 与 refresh token，§2.1 client 类型，§4.4 client credentials；使用时同时遵循 S6 的后续安全修订 |
| S8 | [Web Authentication Level 3](https://www.w3.org/TR/2026/REC-webauthn-3-20260825/)，W3C Recommendation，2026-08-25 | §6.1 / §6.1.1 authenticator data 与计数，§7.1 / §7.2 注册与断言校验，§13.4.3 随机 challenge。Stage 的 intent digest 编码、多方批准、组织签名及治理政策另行定义 |

本文件保留完整目标契约和 I1 场景 ID，随当前范围澄清适用义务。2026-10-25 已实现切片及未覆盖项以[逐项覆盖记录](reviews/p1b-identity-coverage-2026-10-25.md)为准；P1b.4 的本人任职/MFA 新实现与真实协议证据见[本轮记录](reviews/p1b4-assignments-mfa-2026-10-26.md)，不改写此前测试证据或通过数字。没有逐项完整证据的 I1 场景继续保持未验收。开发虚构身份不代表真实企业账号或治理规则已生效。
