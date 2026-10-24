---
title: Stage 身份、认证与授权契约
status: design-proposal
implementation_status: not-started
policy_effect: none
decision_date: 2026-10-24
source_checked_at: 2026-10-24
conformance_suite: stage-identity-v1-draft
---

# Stage 身份、认证与授权契约

本文定义多公司环境下可复用的身份与授权基座，以及蓝图批准所需的身份依据。它是待实现规格，不表示已接入 IdP、实现 Passkey、完成企业身份核验或通过生产安全验收。

本轮一期先建设组件、真实认证协议、当前授权、隔离 WASM 与签名准入，再做基座联调；完整任务业务闭环后置。身份部分通过最小 `EvaluateQualification` 命令验证，不创建真实企业成员、发放岗位或自动授予权限。具体阶段以[一期方案](phase-1.md)为准，产品边界见[愿景与运行时方向](vision-and-runtime-direction.md)及 [AGENT.md](../AGENT.md)。

## 1. 架构决定与当前实现边界

采用成熟的 OIDC / OAuth 身份提供方与协议实现。IdP 负责登录凭据、认证会话、MFA / Passkey 和账户恢复；Stage 负责把认证结果映射到内部主体，并核验公司成员、任职、委托、资源关系和业务政策。生产 IdP 品牌不阻塞接口与本地协议验收。

统一认证来源与授权语义，不要求把所有产品、租户或协议强行放进一个 IdP realm，也不要求一期拆出 IAM 微服务。Maestro / Virtuoso 保持独立产品与客户端注册；Stage 是独立受保护 API。不同公司可在受控信任配置下使用不同 issuer，不能由请求临时指定一个发行方就被信任。

| 责任方 | 权威内容 | 不由它自动决定的内容 |
|---|---|---|
| IdP / 授权服务器 | 登录、凭据、会话、token 签发与自身撤销状态 | 某公司当前岗位、对象读权、独立验收和本次业务批准 |
| Stage Identity | 主体映射、租户成员、任职、委托、服务授权及状态版本 | 替用户保存 IdP 密码，或凭客户端传来的角色建立任职 |
| Stage Authorization | 基于当前可信事实计算能力与资源判定 | 直接产生业务副作用，或把一次允许判定变成永久通行证 |
| Command Kernel / 各领域 | 强制授权、版本与业务不变式；持久执行、审计和结果 | 通过角色名称或“管理员 UI”绕过统一授权入口 |
| Maestro / Virtuoso | 登录交互、上下文选择、受权 API、结果展示 | 自行断言企业管理员，或在客户端复制一套服务端授权规则 |
| Blueprint / Release | 不可变批准对象、批准证据、签名、准入与激活记录 | 用发布批准代替后续每次业务动作的当前授权 |

现有源码的 `PrincipalType` 已区分 `human / agent / service`；`app/authz/context.py` 提供可信输入结构，`app/authz/decision.py` 提供纯判定及委托缩权。真实认证、持久身份模型、可信 loader、授权撤销事务、发布批准与本文新增字段仍待实现。不能将已存在的 dataclass 或枚举算作对应生产能力已经完成。

## 2. 主体、账号、公司与岗位分别建模

### 2.1 主体与外部登录标识

`Principal` 是 Stage 信任域内的稳定主体。`ExternalIdentity` 负责关联外部认证标识；不把 IdP 的用户记录、某次 token、email 或显示名直接当作 Stage 主体。

| 对象 | 建议最小字段与约束 | 权威与用途 |
|---|---|---|
| `Principal` | `principal_ref`、`kind: human / agent / service`、`status`、`identity_epoch`、`version` | Stage 内部主体；`service` 在本文也称 workload 主体，复用现有枚举，不另造同义类型 |
| `ExternalIdentity` | `identity_ref`、`provider_ref`、精确 `issuer` / `subject`、`subject_kind`、`principal_ref`、关联依据、状态与版本 | 经过受信任 adapter 验证后建立；OIDC 身份以 `(issuer, subject)` 识别，[S1] |
| `TenantMembership` | `tenant_ref`、`principal_ref`、状态、有效期、版本；二者唯一 | 加入某公司的事实；不自动赋予公司所有数据的读写权 |
| `HumanIdentityLink` | 受限的人类主体关联、核验来源、可信等级、状态与版本 | 在确有依据时防止同一人换账号绕过独立批准；不是默认收集真实身份证明的许可 |
| `WorkloadRegistration` | `service_principal_ref`、凭据注册引用、允许 issuer / client / 环境、状态、版本 | 将后台服务认证绑定到明确服务；client credentials 不是人类登录，[S7] |
| `AgentRegistration` | `agent_principal_ref`、所属租户、受控执行器引用、责任方、状态与版本 | Agent 身份与运行它的 workload 区分；创建者身份不自动转成 Agent 权限 |

OIDC 只保证 `sub` 在其 issuer 内稳定且不重新分配；email、用户名与同名 `sub` 不能跨 issuer 合并。[S1] 因发行方迁移而建立新映射，必须有受权的关联与复核记录，不直接修改历史审计中的原始来源。

人类 OIDC 身份与 workload 注册使用明确的来源类别。服务 token 即使出现与某个人相同的 `iss / sub` 字符串，也不能复用人类身份查找结果；必须按已登记的 grant / client 语义验证并走 service 注册。无法可靠区分的 provider profile 不接入。

支持 pairwise subject 的 IdP 可能对不同客户端 / sector 给同一个人不同 `sub`。因此 Maestro、Virtuoso 和 Stage 不能靠比较客户端看到的 `sub` 判断是否同一人。[S1] 一期选定受信任 broker / 授权服务器的 Stage API 主体命名空间，adapter 明确 token profile 与映射规则；需要关联其他客户端账号时采用有证明的关联流程。两个 issuer 返回相同 email，即使都声称 `email_verified=true`，也不足以自动合并主体或转移企业权限。

同一主体属于多个公司时，成员关系分别存在、分别撤销。跨公司关联属于受限平台事实，普通公司 API 只返回本公司所需引用与投影，不提供全局人员画像或“此人还在哪些公司”的查询。

独立审批的保证以已核验的主体关联范围为限。两枚 Passkey、两个账号或两个 Agent 不等于两个自然人；无法确定高影响批准要求的身份独立性时，应拒绝满足该要求或转交受控核验，不能用账号数量冒充独立性证明。

### 2.2 岗位、任职、职责与能力

| 概念 | 定义 | 设计规则 |
|---|---|---|
| `PositionDefinition` | 某公司批准的岗位 / 职责定义及来源版本 | 职责说明是政策依据；文本不直接执行成授权代码 |
| `RoleAssignment` | 某主体在某范围内的具体任职 | 含岗位引用、版本化能力角色、scope、有效期、状态与版本；是否允许替补有独立记录 |
| `CapabilitySpec` | 版本化操作契约，如 `qualification.evaluate.v1` | 与[核心对象](core-object-contracts.md)同名；有输入输出、执行预算与所需权限，注册能力不等于给任何人授权 |
| `PermissionGrant` / policy | 在指定任职、服务授权及条件下允许什么操作 | 使用稳定能力 / action 标识，业务 handler 不判断“经理”等名称 |
| `RelationTuple` | 主体与具体资源的权威关系 | 包含 tenant、关系、来源、有效期、撤销状态及版本；拥有部门岗位不推导所有资源关系 |

一次受控命令选择明确的任职。默认不自动并集一个人的全部岗位；跨岗位操作必须由具体政策明确规定组合方式、责任和冲突。容量与独立性仍按实际责任主体计算，不能切换任职复制一份个人容量或给自己验收。

现有内置 `RoleCode → Permission` 映射属于待版本化的政策实现。它可以作为初始可信映射，但不应演化为散落在路由和客户端中的角色名称分支。

### 2.3 有限委托与服务授权

`Delegation` 至少绑定：`delegation_ref`、tenant、Agent、委托人、任职引用、允许 action 与资源 / scope、有效期、是否可转委托、预算引用、状态、撤销时间与版本。P1 只允许直接、不可转委托的有限委托；多级链以后单独验收。

代理动作的有效权限必须同时满足：

```text
委托人当前有效任职及资源权限
∩ 当前有效委托范围
∩ Agent 与承载 workload 的执行限制
∩ 资源政策、认证要求及业务不变式
```

不得把 Agent 独有的资源关系与委托人的另一项岗位权限拼成原本双方都没有的许可。`actor_principal_ref`、`authenticated_workload_ref`、`effective_principal_ref`、`assignment_ref`、`delegation_ref` 和实际责任主体分别记录；服务端核验 Agent 与 workload 的绑定，不能接受客户端自报 Agent ID。

组织自主的后台任务使用独立 `ServiceGrant`，明确 tenant、action、资源范围、责任组织、有效期和环境。它不冒充某个人，也不伪造人类任职。P1 需为现有“必须有 assignment”的纯引擎定义明确的 service 输入分支及验收；不能临时给 worker 塞一个虚构管理员任职。

代表人执行的 worker 则同时持有自己的 workload 认证和有效委托引用。队列只保存业务命令、主体 / 委托引用、原始因果链与幂等身份；不保存用户 access / refresh token，不把入队时的 `allowed=true` 当作执行凭据。

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

同 issuer 的不同 token 用途采用互斥验证规则，避免 ID token、client assertion 和 access token 相互替代。[S5] `scope` 限制 API 的可用范围，但不替代 Stage 内部成员、岗位、资源或当前委托授权。

IdP 的 `auth_time`、`acr`、`amr` 可能不存在或采用不同约定。adapter 记录原始可信含义及映射 profile 版本；不把 `iat` 当作最近登录时间，不把未见认证强度记成 MFA，不按字符串大小比较 `acr`。某动作要求的新鲜认证或强度证据不足时返回需要提升认证或拒绝。[S1]

### 3.3 网络入口、代理和凭据保管

一期基准由 Stage 自行验证 API token。反向代理只转发流量与经过约束的网络信息；外部请求的 `X-User`、`X-Roles`、`X-Tenant-ID`、`Forwarded`、`X-Forwarded-*` 不能建立可信身份或自动决定 WebAuthn origin。

只接受明确登记的代理跳数 / 地址，边缘清除攻击者可注入的安全相关头，代理到 Stage 的通信有认证与完整性保护，后端不能绕过代理直接暴露。如果未来支持代理完成认证，另定义有 audience、短时效、防重放及可核验签发方的内部断言 profile，并测试直连与头注入；不是信任一个普通头即可。[S6]

JWKS、introspection 和元数据地址来自受控 provider 配置。禁止跟随 token 中任意 `jku / x5u` 或未登记 issuer 发起取钥请求；`kid` 是已登记 key set 内的选择提示，不是任意路径或 SQL 片段。[S5]

服务凭据优先采用部署可支持的短期凭据或非对称 client authentication；具体 mTLS / private-key JWT / 工作负载平台能力由 profile 决定。P1 可用隔离本地测试凭据验证合同，生产凭据不能进入仓库、UI 资产、模型上下文、普通日志或 WASM。[S6][S7]

## 4. 可复用数据契约与生命周期

下列字段是待实现的逻辑 schema，命名可在 API 评审时统一，但字段权威与生命周期不得省略。字段缺失必须显式为 unknown / null，不能填造值。

### 4.1 `AuthenticationEvidence`

由受信任 adapter 产生，描述一次入口凭据验证；不是角色清单或可供客户端改写的 DTO。

| 字段组 | 逻辑字段 | 来源与寿命 |
|---|---|---|
| 证据身份 | `evidence_ref`、`adapter_profile_ref/version`、`credential_kind` | Stage 生成关联引用；profile 来自安全配置 |
| 主体来源 | `external_identity_ref`、`issuer`、`subject`、`subject_kind`、已映射 `principal_ref/kind` | 签名 / introspection 结果与受控映射共同确定；未知主体不自动加入公司 |
| 调用对象 | `audiences`、`client_ref`、`sender_binding_ref`、`token_scope` | 仅记录实际验证到的信息；未采用发送者约束时不能声称具备它 |
| 时间 | `validated_at`、`credential_expires_at`、`evidence_valid_until`、可空 `authenticated_at` | 有效截止取凭据、profile 新鲜度和必要在线检查的严格上界；不是延长原凭据 |
| 认证强度 | `assurance_profile_ref/version`、可空 `acr/amr`、`user_verification`、`recovery_epoch` | 只用可核验来源及已登记映射；普通 access token 不当然含 WebAuthn UV 证据 |
| 当前性引用 | `identity_epoch`、`credential_state_ref`、`revocation_checked_at` | 记录本次实际查询到的版本和时间；没有即时撤销通道时明确窗口 |

原 token 仅在入口验证需要的最短期间使用。持久审计记录证据引用、经筛选的事实和验证 profile；需要诊断关联时用受限不可逆引用，不记录 bearer token 或 refresh token。完整 WebAuthn 批准断言另存受限证据库，不进入普通业务日志。

### 4.2 `ExecutionContext` 与可信加载

| 字段组 | 逻辑字段 | 权威与边界 |
|---|---|---|
| 执行身份 | `execution_ref`、`request_ref`、`attempt_ref`、`causation_ref`、`deadline` | 服务端建立；重试 attempt 可变，逻辑命令及幂等身份保持稳定 |
| 认证引用 | `authentication_evidence_ref`、`authenticated_principal_ref`、可空 `authenticated_workload_ref` | 从 adapter 证据与受控运行器得出，不从 body 复制 |
| 业务主体 | `actor_principal_ref`、`effective_principal_ref`、`responsible_principal_ref` | loader 根据直接调用、有效 Agent 委托或 service grant 明确推导 |
| 选择范围 | `tenant_ref`、`membership_ref`、`assignment_ref`、`delegation_ref/service_grant_ref` | 请求可申请选择引用；Stage 验证归属、有效性、范围与版本后接受 |
| 执行来源 | `command_ref/version`、可空 `workflow_run_ref`、`release_ref/digest`、`capability_ref/version`、provider generation | 由命令 / 运行实例绑定，供追踪和一致性验证；不授予业务权限 |

生命周期为一次受控命令 / 消息 attempt。`ComponentContext` 不保存共享“当前用户 / 当前公司”；`ContextVar` 可传 trace，不作为授权权威。进入后台队列、重试或新流程步骤时重新建立上下文。

`TrustedContextLoader` 在当前受控事务中读取成员、任职、关系、委托、主体状态、资源归属、版本、策略与必要资格，生成 `AuthorizationContext`。认证证据说明谁进入系统，当前加载说明这次还能做什么。loader 只读取判定需要的最小权威事实，不应默认加载用户完整档案。

### 4.3 `AuthorizationContext` 与 `AuthorizationDecision`

现有 `AuthorizationContext` 的 subject / action / resource / organization / assignment / relations / attributes / policy / now 保留语义；扩展时追加受控服务授权、事实版本与认证要求，不能把它公开为客户端可提交的“完整可信上下文”。

`AuthorizationDecision` 建议保留现有 outcome、action、subject、resource、policy、evaluated_at、decision_id、request、assignment、delegation，并增加：tenant、effective principal、evidence 引用、授权 epoch、资源与事实版本摘要、必要义务 / 数据投影、稳定原因码及使用边界。

判定只解释指定动作、资源、输入和评估时点。`allowed` 既不预留容量，也不证明业务提交，不是下一请求可重用的 token。`needs_approval / needs_delegation / expired / denied / unavailable` 要分别处理；未知政策、未知认证强度或缺必要事实不能默认允许。

允许判定与执行审计在业务事务中关联。查询、附件下载、搜索、导出、审计读取、SSE 和通知均须有相应的受权投影，不能仅保护写接口。客户端能力预览用于交互，不构成提交时的许可。

### 4.4 共用模块，不复制政策

P1 在 Stage 内以清晰模块组织 `authentication → identity loader → authz → command enforcement`。HTTP、CLI、Agent、worker 使用同一受控查询与命令路径，各领域负责强制检查自身入口和不变式。

Maestro / Virtuoso 共享版本化 API schema、生成 typed client 的来源、上下文选择协议、错误码和兼容测试。SDK 不内置公司角色规则，不返回可在另一个服务中脱离当前事实使用的 `allowed=true` 授权票据。

`AuthenticationAdapter`、key resolver、identity repository、授权引擎可作为受信任组件服务装配；provider 的替换遵循 Core generation / drain 规则。组件替换本身不继承或扩大用户授权；认证与强制授权服务不得由普通业务包覆盖。后续若拆分服务，须另定受众、传递凭据、撤销一致性与决策版本协议。

## 5. 一期 `EvaluateQualification` 如何贯穿身份边界

该命令只生成受权资格评估记录。WASM 返回的数据是有限规则的计算结果，不是 `AuthorizationDecision`，不能自行建立成员、任职、委托或批准发布。

```text
入口验证真实凭据，映射主体，核验租户选择
→ 固定可信 command handler 确定评估对象、规则包及输入 schema
→ 短事务：按一期锁序持guard与FactSet锁，一致读取当前权限、最小事实与revision/digest
→ 结束事务，交独立 WASM runner 做有预算的纯计算
→ 新短事务：锁授权 guard 与目标，重载当前身份 / 权限 / 规则准入
→ 重验凭据期限、deadline、事实 version / digest 和输出 schema
→ 固定 handler 判断结果可否接纳
→ 同事务保存评估结果、decision、审计和 outbox
```

读阶段的允许不能覆盖提交阶段。计算期间撤销成员 / 委托、规则包被撤销、输入事实变化或凭据到期，最终事务必须拒绝或返回可重试冲突；不得沿用旧 `allow`。WASM 执行期间不持数据库事务锁，也不携带用户 token、完整 `AuthenticationEvidence`、数据库句柄或可扩大权限的通用宿主调用。

若当前授权不再允许读取原计算结果，拒绝响应不回显敏感输入或结果；审计保留受限摘要与原因。一个可信业务 handler 可以依据当前规则使用评估结果，但不得把模块输出的“合格”解释成获得任意业务执行权。

## 6. 撤销、缓存、密钥与账户恢复

### 6.1 分开四种撤销

| 变更 | 即时权威与处理 | 不能假称的保证 |
|---|---|---|
| token / IdP session 撤销 | provider 的撤销、introspection 或已验收事件协议；本地凭据阻断记录 | 仅验证自包含 JWT 签名，不能得知 IdP 刚刚撤销了该 token，[S3][S4] |
| Stage 主体 / 成员 / 任职 / 委托撤销 | 当前 Stage 记录、版本与授权 guard；新受控动作重读 | 不等 token 到期才失效，也不倒改撤销之前已提交的业务事实 |
| IdP / workload key 撤销 | 受控 key 状态、可信 key set 与紧急阻断 | `kid` 更换不等于账户撤销；从 JWKS 移除 key 也不是自动撤销全平台会话的协议 |
| 发布密钥 / 包 / 批准撤销 | 发布信任配置、批准状态、包状态与准入版本 | 历史签名仍可密码学验证，不表示现在允许激活或继续业务执行 |

普通 key rotation 支持新旧 key 的明确重叠期：可信 JWKS 缓存有界，未知 `kid` 至多触发受控刷新和限流，仍未知则拒绝。算法与用途由配置限定，不由 token 指定。发生疑似泄露时通过 key / issuer 阻断与新配置版本使旧缓存失效；不能仅等缓存自然过期。[S1][S5]

验证网络不可用时，不为未知 key、过期元数据或缺必要在线状态降级放行。已验证的缓存是否仍可使用必须有明确 profile 与最长时效；超出窗口返回不可用。introspection 的成功缓存不会超过 `exp`，同时承认缓存期间的撤销延迟；要求即时性的动作不用跨请求成功缓存。[S3]

远端取钥 / introspection 在业务事务之外完成；锁内重验本地身份状态、证据时效与其记录的撤销版本，不持数据库锁等待 IdP 网络响应。即使禁用跨请求缓存，远端查询完成到本地提交之间仍有窗口；若某动作要求跨系统即时撤销，需另定在线授权协议或等价机制，不能从本地 guard 推导出跨 IdP 的原子性。

### 6.2 当前授权缓存与并发

沿用一期每租户 `AuthorizationGuard` 协议：成员、任职、关系、委托、服务授权、相关主体状态和有效策略变更与受控业务写按共同锁顺序串行化，增加 epoch。资源版本、幂等和领域锁仍保留。最终授权时间点在取得必要锁后、业务写之前；命令 deadline 与当前凭据时效同样重验。

写命令不复用跨命令的正向授权结果。若查询引入缓存，key 至少包含 tenant、主体及有效主体、任职 / 委托、action、资源 / 投影、策略版本与 epoch，另有过期与失效机制；缓存命中不免除入口认证。负向缓存也要有界，避免新授权长期无法生效。

全局主体禁用、账号重新关联或平台共享策略变更可能影响多个租户。必须明确受影响集合并按稳定顺序锁定全部 guard，或采用单独已经验收的全局身份撤销协议；P1 若不能建立完整集合就拒绝该全局变更，不声称只锁一个公司即完成全局撤销。

排队动作、长期人工等待和恢复后的步骤都重新判定当前权限。历史批准是一项事实，执行时是否继续认可它由明确发布 / 业务政策决定；离职人员旧批准的法律效力不由本技术规格推断。对正在进行的外部 I/O，本地撤销只能记录取消意图与实际结果，不能宣称原子收回已经发出的请求。

### 6.3 账户恢复与身份关联

IdP 恢复登录能力不自动恢复 Stage 已撤销的任职、委托或发布签名能力。涉及恢复事件的认证证据标记可核验的 recovery 状态 / epoch；高影响批准按政策要求重新确认有效凭据和新鲜认证。

新增 / 替换 Stage Passkey、合并主体、迁移 issuer、转移 workload 所有权均走受控命令与审计。已有任职不随 email 修改转移到另一个主体；账号重建后获得新 `sub`，也不自动继承旧主体。

遗失全部凭据时走独立恢复政策：核验谁可恢复、如何通知与撤销旧凭据、怎样解除临时限制。P1 验证真实协议登录以及虚构账户的恢复 / 撤销边界，不交付真实自然人实人核验或默认安全问题兜底。应急账号如以后引入，必须独立、限时且审计，不能被业务蓝图创建或启用。

## 7. Passkey 批准、组织签名与执行准入

### 7.1 两类签名的职责

WebAuthn 基础认证断言签署的是 authenticator data 与 client data hash 的组合；challenge 在 client data 中被绑定。它不是“调用 Passkey 私钥直接签任意 WASM 文件”的通用接口。[S8] 普通 2FA 登录成功也不等于用户签署了某个业务变更。

本文采用“Passkey 形成具体批准证据，组织发布密钥签发布清单”的方案。两类密钥、用途和验证链独立：

| 环节 | 要证明的内容 | 结果 |
|---|---|---|
| 人员认证 | 当前进入系统的是哪个受信任主体 | `AuthenticationEvidence` |
| 特定发布批准 | 此主体以有效批准权限确认了这个不变意图 | `ApprovalEvidence`；不直接授予运行时业务权限 |
| 组织签名 | 发布服务依据有效批准政策认可这份完整清单 | `ReleaseEnvelope`，由组织发布 key 签署 |
| 激活 / 运行准入 | 该包目前可在此 tenant / 环境使用，能力与依赖符合约束 | `AdmissionDecision / ActivationReceipt` |
| 每次业务执行 | 当前 actor / 委托及资源事实允许具体动作 | 当前 `AuthorizationDecision` 与命令结果 |

P1 用隔离开发签名 key 验证协议，key 由 runner 外的受控签名方持有。生产使用 KMS / HSM 或同等可审计密钥设施的具体 provider 待选；不因此推迟发布清单、key 用途隔离及准入验收，也不把开发私钥当生产方案。

### 7.2 不可变批准意图和一次性 challenge

`ApprovalIntent` 至少冻结以下内容，并使用版本化规范编码求摘要：

- `intent_ref / schema_version`、tenant、环境、明确批准动作、申请人及来源；
- 发布清单 digest，覆盖 DSL / 规则 / WASM digest、依赖锁、宿主接口、能力上限、预算和配置绑定；秘密本身不写入清单，秘密使用受控引用与绑定版本；
- 目标发布序号、期望前置活动版本、批准政策版本、风险 / 影响摘要引用；
- 提交与批准截止时间、适用范围、所需独立批准者及允许的认证 profile。

选择的 digest 算法和编码版本由发布规格固定；不得对任意语言中未经规范化的 JSON / YAML 字符串默算摘要。可读语义 diff 与被批准清单必须引用同一不可变对象。

建议 P1 的直接 WebAuthn 方式采用 Stage 自己登记的 RP credential。IdP 域名下的 Passkey 不会因用户登录 Stage 就变成 Stage RP 的凭据；两个方案需分别配置和验收。

1. 管理员请求批准某个冻结 intent；服务端先确认身份、tenant、选定任职与当前批准能力，显示这份 intent 对应的摘要和差异。
2. 服务端生成至少 32 字节安全随机 nonce，生成 challenge，例如 `nonce || SHA-256(domain_separator || intent_digest)`；这是 Stage 协议选择，不是 W3C 规定的业务编码。challenge 记录绑定 intent、批准主体、会话 / ceremony、用途、过期时间与状态。
3. RP 发起 `webauthn.get`，要求 `userVerification=required`；批准 credential 必须已经通过受控注册绑定该主体。不能只信前端声称“已验证指纹”。
4. 成熟 verifier 检查签名、预期 challenge、`type`、精确允许 origin、RP ID hash、credential 归属、UP / UV 与所用扩展规则。P1 不接受跨 origin iframe 的批准 ceremony；代理头不能改变允许 origin。[S8]
5. 在短事务中按[一期统一锁序](phase-1.md#transaction-protocol)取得相关TrustState/主体行、tenant guard，再锁intent/challenge/批准记录；重验凭据状态、新鲜度、信任revision、批准能力、时间、政策和意图一致性，一次性消费challenge并写证据。并发重复提交不能形成第二份批准。
6. 超时、重放、修改环境 / digest / 动作、凭据撤销或任职失效均拒绝；意图改变必须新建 intent 并重新批准。已记录结果的响应丢失可受权查询，但不重新消耗 challenge 或增加批准票。

WebAuthn `signCount` 是克隆风险信号，部分认证器可以恒为零；它不能代替一次性 challenge。[S8] 若允许同步 Passkey，不能宣称凭据天然不可复制或严格绑定某一硬件；设备 / attestation 限制须成为单独的明确政策与验收。

普通 WebAuthn 用户验证不保证认证器屏幕可信展示了完整业务变更。冻结对象、页面内容与摘要一致、防止跨站请求和 UI 注入、可靠的 origin 验证仍有必要。批准记录只能声称已验证的事实，不能因指纹弹窗而声称管理员已阅读或理解全部变更。

如果改用 IdP step-up，应将新的认证事务通过服务端保存的 `state / nonce / intent_ref` 绑定到原不变意图，验证 callback、所需 `auth_time / acr / amr` 和当前批准权后写证据。它证明受绑定的提升认证与业务批准流程，不等同于 Stage 持有可独立验证的“用户对制品的裸签名”；不得把这两种证据格式混用。[S1]

### 7.3 多人批准、签名与不可自改的信任根

多人批准是业务政策，例如要求两个具备指定能力且互相独立的人批准同一 intent。阈值密码签名则是多个密钥份额共同产生一个签名的密码协议。二者可以组合，但不能把保存两条批准记录称作已实现阈值签名。

P1采用固定单批准人政策，检验有权主体、意图绑定及重复证据不增加批准次数；双人正向流程留后续治理验收，不实现阈值密码协议或通用审批UI。同一人换账号、任职、Agent或Passkey不应在未来多方政策中变成独立主体；申请、批准、发布、激活权限分别定义，待批准包不能把规则改低。

发布服务在签名前重新确认 intent 未变化、批准政策版本仍可用、所需批准未撤销并且符合当前政策。原始断言、credential 公钥 / key 标识、intent digest、批准主体及任职、验证配置版本和时间作为受限证据保存。变更签名内容或追加未批准权限必须走新 intent。

对外签名调用也有独立请求身份与结果记录：批准决策、签名返回和激活不能假装是一个跨 KMS / 数据库的原子事务。响应未知时先按原签名请求对账；已产生的有效签名仍须通过当前准入，签名期间发生的撤销不得被一次早先允许结果覆盖。

IdP trust、认证 adapter 配置、组织根 key、验签开关、可签用途、批准规则、身份关联与授权强制入口属于平台安全配置。普通业务蓝图、WASM 和组件配置不能替换它们。key 轮换、根信任变更及恢复由现行可信规则批准并保留明确过渡；新 key 不能仅凭自己签一份声明就成为信任根。

业务发布序号不因回滚而降低；回滚引用旧内容 digest，但形成新的受权发布动作。历史合法签名不绕过当前包撤销、环境匹配或防降级检查。停止接受新实例、在途步骤怎样处理和是否需人工接管应由发布规格区分，不能只定义一个含糊的“撤销签名”。

## 8. 隐私、最小输入与可追责

“重新加载当前身份”指获取当前判定必需且可信的状态，不是每步获取完整个人资料。普通资格评估可只需本租户主体引用、有效成员、某项资格证明、委托范围和版本；不必取得私人会话、全部任职、出生信息或其他公司关系。

对外投影采用 tenant 范围内的必要标识；用于防自批的内部主体关联由受限服务比较，向一般审批 UI 可只返回“独立性满足 / 不满足 / 无法确认”及受权原因。审计既要能定位责任，也要有读者范围和数据保留政策，不默认公开全局身份映射。

WASM 输入采用 capability schema 规定的最小投影，宿主在输入前授权、输出后校验与最终重验。P1 纯计算模块不能拿到用户 token、刷新凭据、Passkey 私钥、组织签名 key、完整组织目录或数据库连接。后续宿主能力接口也只接受受限句柄和类型化请求，由宿主重新授权，不能把请求中的 tenant / actor 当事实。

后续若引入可验证凭证或选择性披露，可信 loader 可验证“此主体在该挑战下具备资格”等最小证明及其当前性。不得在验证最小证明之后又无条件取回完整身份档案；同样不能用匿名证明掩盖本动作明确要求的责任或独立批准条件。证明绑定、发行方、撤销、重复使用和不可关联性需另立协议，本文件不声称已经实现。

## 9. P1 交付与验收

### 9.1 范围与退出条件

| 阶段 | 本文承担的交付 | 退出门槛 |
|---|---|---|
| P1a 共同契约 | 主体、外部身份、成员、任职、委托、服务授权；三类上下文与 decision schema；组件与身份 provider 边界 | 语义、来源、失效及与 Command Kernel 的使用约束通过评审 |
| P1b 真实协议与当前授权 | 至少一个成熟本地测试 IdP；真实授权码 / API token 验证；JWT 与 opaque adapter 合同；当前 loader、tenant 隔离、撤销及 fixture 门禁 | 实际协议请求与固定错误输入可复现；不能仅用 fixture 构造 `AuthenticatedPrincipal` 代替 |
| P1c WASM / 发布接口 | workload 到 runner 的身份边界；批准意图、开发签名 key 分离及准入所需身份 / 审批证据接口 | token 不进入 WASM；有限发布对象通过有效批准与签名准入；不建设通用蓝图编辑器 |
| P1d 基座联调 | 真实 token → tenant scope → `EvaluateQualification` → 纯计算 → 当前重验 → 结果 / 审计 / outbox；最小 Passkey ceremony | 正负场景分别留存证据；虚构主体及开发 key；生产 IAM 验收单独记录 |

JWT / opaque 两类合同均定义清楚，P1 至少选择一个实际支持的 profile 完成真实协议链路。未选 profile 必须明确拒绝，不能宣称支持；相应场景记录为不在本次支持范围，不能计通过。以后增加该 profile 时，须执行对应真实网络协议测试；受控测试服务器与真实 IdP 的覆盖来源分别标注，不能把通用 mock 的 `active=true` 称作某个 IdP 已支持。支持列表只列实际验证的 provider / profile 组合。

Passkey P1 可使用浏览器虚拟认证器验证 RP 注册、challenge 绑定与断言拒绝；这不证明真实硬件、跨设备同步、企业 attestation 或账号恢复体验已经验收。测试 IdP、浏览器配置和开发 key 只存在隔离测试环境；本文不安装依赖、不配置真实 IdP、不创建生产账号。

### 9.2 `I1` 验收场景

以下 **36 个待实现场景，已执行 0、已通过 0**。每项分别记录正向与反向证据、运行 profile、数据库 / 浏览器 / IdP 版本和未覆盖范围；不与已有 Runtime Core 的场景数量相加冒充已通过测试。场景 ID 在后续拆分时保留映射。

| ID | 场景与正向预期 | 反向 / 并发预期 |
|---|---|---|
| I1-01 | 本地测试 IdP 完成授权码 + PKCE 登录，正确 state / nonce 回调建立会话 | 错误 state、nonce、重定向或复用 code 拒绝；无 fixture 旁路 |
| I1-02 | 符合已登记 profile 的 JWT access token 可访问 Stage | 错 issuer / audience、过期、nbf 未到、未允许算法、坏签名拒绝 |
| I1-03 | 已登记 opaque token 经实际 introspection 得到足够可信事实 | inactive、到期、缺必要 audience / subject、端点失败不默认放行 |
| I1-04 | 登录客户端验证自己的 ID token；API 使用独立 access token | 产品 ID token、refresh token、client assertion、发布签名不能替代 API token |
| I1-05 | JWKS 正常轮换在明确重叠期接受合法新旧 key | 未知 kid 刷新有界；恶意 jku / x5u 不访问；被禁 key 与过期缓存拒绝 |
| I1-06 | profile 明确解释 auth_time / acr / amr，满足新鲜度要求 | 把 iat 充当 auth_time、缺证据当 MFA、未经映射 acr 均拒绝高要求动作 |
| I1-07 | 已登记代理链转发后能校验固定 origin 和原始请求边界 | 伪造身份 / forwarded 头、直连绕过、额外代理跳不建立可信身份 |
| I1-08 | 同一已核验 issuer/sub 稳定映射一个主体 | 同 email 或同 sub 不同 issuer 不合并；未验证账号关联不迁移权限 |
| I1-09 | pairwise 标识通过受控 broker / 明确关联映射到相应主体 | 不同 client 的 sub 不盲并；伪造关联或未证明同人不满足独立性要求 |
| I1-10 | 一个主体的 A / B 公司成员关系分别有效 | A 请求使用 B 资源、成员引用或作用域拒绝；全局 ID 不替代 tenant 过滤 |
| I1-11 | 同一人选择明确任职执行该范围能力 | 客户端自报 role、切换岗位自动并权、岗位过期或越 scope 拒绝 |
| I1-12 | 当前资源关系与任职能力共同满足才允许 | 仅有公司成员身份、旧关系或别人的资源关系不能获得权限 |
| I1-13 | 有效有限委托允许 Agent 在范围内代表人执行 | 改 Agent、委托人、任职、资源、动作、预算或到期范围拒绝；不可转委托 |
| I1-14 | 受信任 workload 认证并与 Agent 运行绑定，保留双方身份 | 单独提交 Agent ID、拼接无关关系、假冒人类 sub 或冒用 client_id 拒绝 |
| I1-15 | 独立 ServiceGrant 允许明确后台任务 | worker 进程不获得隐式管理员；不得用虚构人类任职绕过 service 合同 |
| I1-16 | 人、Agent、service 输入均产生来源明确且不可变的认证证据 | body 覆盖 evidence / tenant / actor / assurance 失败；原 token 不持久写日志 |
| I1-17 | 并行请求各自构造 tenant、主体、Session 与 provider 租约 | 组件全局变量、ContextVar 或前一请求上下文不能泄漏权限到后一请求 |
| I1-18 | HTTP、CLI、worker 走同一命令与授权合同 | SDK allow 缓存、内部直调 handler、未核验消息身份不得绕过强制入口 |
| I1-19 | 查询 / 导出 / 附件 / 审计 / 通知返回允许投影 | 通过搜索、SSE、旧幂等结果或错误详情读取无权数据被阻断 |
| I1-20 | 最终事务记录当前 decision、事实版本、执行结果和审计 | 旧 decision ID 或模型 / WASM 输出不能作为下一次授权 token |
| I1-21 | 命令先持 guard 并提交，审计顺序清楚 | 撤销先提交时随后命令必须拒绝；两个数据库连接覆盖真实锁竞争 |
| I1-22 | 队列消息由当前 workload 认证并重载有限委托 | 入队后离职、委托撤销、deadline / token 到期均阻断；不使用保存的用户 token |
| I1-23 | 缓存命中仍受 epoch、profile 时效与 token exp 限制 | 更换政策 / key / 成员状态后旧缓存不越权；网络失败不延长证据有效期 |
| I1-24 | 全局主体变更覆盖完整受影响租户并按稳定锁序处理 | 只锁单租户或无法确定影响集合时拒绝全局禁用 / 关联变更的提交 |
| I1-25 | IdP / Stage 凭据恢复后仍需当前任职与适用认证强度 | 恢复登录不复活旧委托 / 已撤销岗位；email 转移不转移历史主体 |
| I1-26 | Stage RP 注册凭据绑定已认证主体并验证注册 ceremony | 攻击者替换 credential / 主体、origin 或 RP ID 拒绝；IdP RP 凭据不默认复用 |
| I1-27 | Passkey 对冻结 intent 的一次性 challenge 产生有效批准证据 | digest、tenant、环境、动作、前置版本变化或 intent 过期必须重批 |
| I1-28 | 正确断言满足签名、challenge、type、origin、RP ID、UP / UV | 普通登录 MFA 标记、前端 UV 标记、坏签名、错来源不能批准 |
| I1-29 | 同一 challenge 成功消费一次，响应丢失可查询原结果 | 并发重放仅一条批准；signCount 为零不取消 nonce 防重放机制 |
| I1-30 | 固定批准政策被正确执行，P1采用一个有权批准人 | 重复证据不增加次数，不能降低政策或让无权主体批准；双人正向留后续，不计P1通过 |
| I1-31 | 独立发布服务复核批准后用 runner 外开发 key 签清单 | WASM、普通 handler 或凭单一登录态不能直接调用无限制签名接口 |
| I1-32 | 可信配置明确轮换 key、发布撤销及受权回滚 | 业务包自换根 key / 关闭验签 / 降低自己的批准要求 / 重放旧发布序号拒绝 |
| I1-33 | 资格评估只向 WASM 提供受权最小投影并接纳符合 schema 的结果 | token、完整身份、跨租户事实不能进入 guest；guest 声称 allow 不授予业务权 |
| I1-34 | 纯计算后当前事实与权限未变，原子保存评估和审计 / outbox | 计算间撤销、事实版本变化、包撤销或认证过期，提交拒绝 / 冲突且不泄露旧结果 |
| I1-35 | fixture 显式启用时仍走真实 loader / policy，协议集成单独记录 | prod 装配 fixture 拒绝；fixture 通过不能代替 IdP / Passkey 协议通过 |
| I1-36 | 只加载判定必要状态与资格证明，受限审计可定位责任 | 不取私人会话、全局公司关系或无关档案；最小披露不被全量重载抵消 |

阶段分组：P1b验收I1-01～25、I1-35～36的适用项；P1c/P1d补I1-26～34并执行全链集成。I1-02与03按实际支持token profile记录，未选类型必须验证拒绝且不可记为正向通过；JWKS等profile特定子项同样记录适用性。P1默认Stage RP直接WebAuthn ceremony；若另选IdP step-up，须另列等价批准证据用例并明确I1-26～29的未覆盖范围，不能直接声称通过该组。

## 10. 待决项与生产门槛

这些选择需要在相应实现前固定，不以“后续再说”绕过验收；它们不阻塞当前设计与隔离基座开发。

| 待决项 | P1 必须固定 | 正式部署前另验收 |
|---|---|---|
| IdP / 客户端拓扑 | 本地测试 IdP、支持的 JWT / opaque profile、Stage audience、回调、client 类型 | 每个生产 issuer 的合同、可用性、账户生命周期与恢复责任 |
| 主体映射 | broker 命名空间、pairwise 处理、关联状态和虚构同人映射 | 跨 IdP 人员关联与独立批准所需的真实核验强度 |
| 认证新鲜度 / 撤销 | 各证据时效上限、缓存、未知状态失败语义、测试撤销窗口 | 生产撤销事件 / introspection SLA、受影响会话和在途动作政策 |
| Passkey 批准 | Stage RP 或 IdP step-up 的明确 profile；P1 默认直接 Stage RP 的隔离验收 | 真实 origin、同步凭据策略、账户恢复、硬件 / 移动端及可访问性 |
| 发布签名 | 版本化清单、开发 key 隔离、批准与 key 用途、撤销 / 防降级 | KMS / HSM provider、生产密钥恢复、根信任轮换与运行职责 |
| 人工责任与隐私 | 虚构批准政策、最小输入 schema、证据读者范围 | 公司正式批准政策、资料保留 / 删除、跨境与外部目录同步安排 |

尚未选择或测试的 provider、浏览器、密码库、发送者约束、阈值签名、隐私证明和多节点撤销协议不列为已支持能力。生产部署必须在实际 IdP 与入口拓扑完成独立协议和运维验收，不以本地测试 IdP 或当前设计文档替代。

## 11. 官方依据与核对范围

以下均于 **2026-10-24** 实际读取官方正文。标准规定与本文 Stage 方案有意分开：`AuthenticationEvidence` 字段、主体模型、guard、批准清单与 I1 场景均是 Stage 自己的待实现契约，不是这些标准授予的认证。引用用于支持列明的精确范围，不宣称某个库或 IdP 已完整实现标准。

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

本文件是设计落档：没有新增认证 / 业务代码、API、数据库迁移、真实账号、IdP 设置或依赖；没有运行 I1 场景。下一步实现需同时更新协议 schema、契约测试与上述逐项证据。
