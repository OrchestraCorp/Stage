---
title: Stage 一期基座架构与验收
status: design-proposal
implementation_status: p1b4-assignments-login-mfa-server-implemented-client-and-full-p1-pending
policy_effect: none
revised_date: 2026-10-26
---

# Stage 一期基座架构与验收

部署目标已收敛为**一个组织授权根一套 Stage、一套数据库、一个环境**。本期不建设多租户选择/管理；根是信任与治理边界，不等同于一个法人公司。集团、子公司、部门等业务组织单元以后按需建模，其访问范围可由 scope 表达，但 scope 不自动等同组织树。原 `tenant_*` 字段及 guard 保留兼容并绑定唯一组织根，以下边界验收以根单例、主体、任职与 scope 为准。公开业务接口统一 `/api/v1`；详见[组织授权根语义](organization-root-semantics.md)和[私有化部署与 API v1](private-deployment-api-v1.md)。历史验收记录中的多公司 fixture 不再作为当前产品范围。

一期按组件运行时、真实人员身份与当前授权、受控 WASM 和签名准入的共同基座推进。P1a 已有生命周期、PG/Redis provider 与内部命令/投递底座；P1b.1/.2 已有真实身份与单任职/角色版本/scope/resource/直接 ACL。P1b.3 接通人员证据到提交前重验，以已有 WorkItem 标题/说明编辑和结果查询验证最小闭环，见[P1b.3 记录](reviews/p1b3-human-work-2026-10-25.md)。Stage 最小路径收尾不等于完整 P1b：[P1b.4](reviews/p1b4-assignments-mfa-2026-10-26.md)已交付本人任职发现和组织统一登录 MFA 服务端能力，密码＋TOTP 经隔离真实 IdP 与 Stage 链路验证；随后原生认证模块补齐服务端因素绑定/恢复与会话管理；真实 Maestro 客户端与剩余适用 I1 项仍待验收；正式人员与权限管理仍缺产品接口，开发 CLI 不替代它们。WASM 与签名继续按阶段推进；完整任务业务保留到[后续业务切片](business-slice-plan.md)。

早期协作模型确定为**人员—公司—人员**，Agent 是人员监督使用的工具，不作为独立成员、任职者、承诺者或验收者，不建设 Agent 专属业务接口。当前 HUMAN 授权 loader 符合此边界。独立 Agent 身份、承载绑定和 Delegation 不属于一期门槛，也不预排到后续固定阶段；ServiceGrant 仅在真实技术后台任务需要时另案设计。已实现的 service 认证继续保留为技术来源验证，不能据此取得人员业务权限。详见[人员—公司—人员协作边界](people-company-collaboration.md)。

本文件是一期范围的权威入口。对象定义见[核心对象](core-object-contracts.md)，身份协议见[身份与授权](identity-and-authorization.md)，WASM 见[宿主设计](wasm-host-design.md)，历史表达修正与来源见[核对记录](design-review-2026-10-24.md)。

2026-10-25 的[框架实施规格](framework-foundation.md)已完成 P1a.1～P1a.3：原 Core 34 个场景加 12 个 S1 边界场景映射验收 46/46，证据见[P1a 验证](reviews/p1a-validation-2026-10-25.md)。这些结果不替代后续身份、WASM 和联调验收。

## 1. 一期目标

一期验证一个共同基座：**自研组件运行时管理可信宿主与受控 runner；真实协议凭据建立可信身份；签名模块在最小权限和预算内计算；宿主依据当前授权与事实版本提交可对账结果。**

交付范围：

- 自研 Python async Core：声明依赖、provider、资源 owner、代次、就绪、租约、排空及失败报告；完成原定 34 个 Core 场景和补充 12 个 S1 边界场景。
- 能力、身份上下文、命令/结果、发布清单、runner 调用等共同契约；未来 WorkflowRun 的边界一并定义。
- 至少一个真实认证协议 adapter 对本地测试 IdP 的集成路径；闭合人员身份、登录 MFA、当前成员、本人任职发现与选择、资源关系、撤销竞争和真实认证证据到命令的重验。已有技术服务认证单独保留验证，fixture 仅辅助隔离测试。
- WASM 宿主与独立 runner 进程：固定标准 Core WASM / ABI、纯计算、有限输入输出、无任意宿主导入、受控编译与执行预算。
- 最小制品批准与签名准入：冻结 manifest、授权批准记录、验签/摘要/环境/版本/撤销检查，受控激活；不要求通用发布画布。
- 一个 `EvaluateQualification` 资格评估演示命令，串联当前身份、真实受限输入、WASM、提交前重验、幂等、审计、outbox 与恢复。
- 生命周期与故障验收：runner 崩溃/超时、provider 换代、身份或发布撤销、输入变化、重复请求和提交回执丢失。

一期暂不交付：通用工作流 DSL / 画布、完整任务与容量产品、生产任意插件上传、进程内源码 HMR、分布式共识、跨组织原子事务、隐私证明系统或多种密码学方案。生产原生认证部署与恢复责任、KMS 和运行平台选择待确定；本地协议和签名验证不能因此只留空接口。

## 2. 部署与信任边界

**单个 Stage 宿主、单事件循环、单 ASGI worker，加受控 WASM runner 子进程。**这里的 runner 是隔离执行边界，不是多个共享内存的 ASGI worker。CPU 编译、实例化及执行不得堵塞宿主事件循环。

| 边界 | 可持有的资源 | 不可越过的约束 |
|---|---|---|
| Stage 宿主 | 经过核验的 Python 组件、数据库工厂、身份/信任适配、当前授权事实 | 同进程 Python 组件属于受信任代码；Context 不是沙箱 |
| 签名服务 / 开发签名工具 | 开发环境专用私钥或生产外部签名接口 | 私钥不交给 runner、WASM、浏览器包或普通日志；发布需要现行批准规则 |
| 测试 IdP | 虚构账号与真实协议端点/凭据 | 与真实生产企业目录隔离，认证强度和恢复边界按身份文档说明 |
| WASM runner | 固定引擎配置、指定原始模块、必要输入、预算、受保护 IPC | 无数据库/IdP bearer token/签名私钥，不继承可用的环境秘密、文件描述符或任意文件网络能力 |
| WASM guest | 本次有限输入、私有内存、结构化输出 ABI | P1 不提供任意 filesystem/network/clock/random/DB imports；输出不能修改宿主授权 |
| 测试投递 sink | 虚构、可控的事件接收端 | 支持重复、超时与已接收但回执丢失；不连接真实外部业务 |

仅创建子进程不等于完成 OS 隔离；具体运行平台必须验证环境净化、文件描述符关闭、权限、资源限制和终止回收。生产平台未验证前只能报告本地基座就绪。

## 3. 组件装配和共同对象

一期目标的受信任 composition root 装配 RuntimeHost、固定能力/命令实现、认证适配、可信 loader、authz、CommandKernel、UoWFactory、制品信任、WasmExecutor provider 和 outbox worker。当前仅固定 CommandSpec 集合与人员元数据服务已接通，WASM/发布仍待实现，不要求先建通用能力注册平台。启动必需依赖不满足时 readiness 失败，已有 `/health` liveness 含义保持。

WasmExecutor provider 管理引擎配置和 runner 监督；每次调用获得代次租约。退役先拒绝新调用，再等待或取消纯计算并确认 worker 已结束，最后释放资源。未确认终止时不能报告成功清理。原 Core 的 default drain 规则不因引入 runner 被放宽。

可信业务服务、命令集合和规则实现可按领域模块组合，独立资源/生命周期的服务由插件管理；不是每个函数或持久记录都需要一个插件。运行时装配不授予业务权限，也不允许业务插件自改平台信任配置。

AuthenticationEvidence、ExecutionContext、AuthorizationContext 分别描述已验证认证、一次执行的可信身份引用、当前动作内的授权投影；不能存为共享 provider 的当前用户。能力注册与获得服务对象也不等于主体获得业务权限。

P1 在接口评审时定义后续 WorkflowDefinition/Run/Step/Wait 的引用关系，但不用固定演示命令冒充通用工作流引擎。P2/P3 的蓝图与流程都复用一期能力与命令契约。

## 4. P1a～P1d 分段交付

| 阶段 | 主要交付 | 退出门槛 |
|---|---|---|
| P1a：Core 与共同契约 | 按 P1a.1资源依赖 → P1a.2安全退出与组合 → P1a.3应用装配交付；Core 不依赖身份/业务类型 | C1–C8 共 34 项＋S1 共 12 项、共享语义与 D-01～D-09 证据、真实 provider 和既有回归；此时不宣称 IAM 或 WASM 已验收 |
| P1b：真实身份与当前授权 | 实际协议 adapter、本地测试 IdP、人员身份映射、登录 MFA、本人任职发现与选择、成员/关系、guard 与认证证据、命令重验 | 客户端及登录 MFA 端到端验收、当前人员范围适用 I1 项；明确正式人员/权限管理缺口。独立 Agent、Delegation 与 ServiceGrant 后置且不计通过；绑定意图的批准和签名/WASM 在 P1c/P1d 验收 |
| P1c：WASM 与制品准入 | 固定 ABI、独立受控 runner、原始模块验证编译、预算、IPC、批准签名清单、受控激活与撤销 | W1中准入/ABI/预算/IPC/生命周期场景通过，非法制品/权限/imports/预算均拒绝，真实签名验证而非mock allow；业务联调项在P1d完成 |
| P1d：基座联调 | EvaluateQualification、提交前重验、幂等/审计/outbox、多主体/部门 scope 并发、升级与故障恢复、运行手册 | W1-01～28、所选profile适用I1项、O1-01～07与F1-01～12通过；O1-08留P3。未选profile不可计通过；生产部署另验收 |

协议可以并行设计，实现与验收按依赖推进。本地认证已选择[自研最小模块](native-authentication-design.md)，旧隔离 IdP 用于迁移比较与兼容测试；WASM 引擎的精确版本、平台能力和预算必须在实现前锁定，不能用“latest＋无限制”代替决策。

P1b 内部按[实施入口](p1b-implementation-entry.md)四个切片推进，表结构以[已对齐设计](identity-schema-design.md)为准。首条认证/成员读取和元数据编辑切片均不等同完整 P1b；登录 MFA 不以 WASM 或业务批准实现为前提，登录因素已选密码＋TOTP，组织策略绑定精确 profile/version、ACR/AMR 和认证年龄；真实桌面与生产接入仍分别验收。真实签名 token 验证测试与成熟 IdP 授权码＋PKCE 联调分别记录，不能互相代替。

## 5. 最小垂直切片：EvaluateQualification

此命令用于证明三类基座能够共同运行，结果是资格建议与原因，不生成任职、不授予权限、不提交真实业务批准。

```text
经过真实协议认证
→ 校验部署组织/主体/范围和评估能力，取得固定模块/handler绑定
→ 创建幂等执行记录，读取受限 FactSnapshot
→ 结束读取事务，提交本次 runner 调用
→ 验证 invocation、attempt、模块/输入摘要与结果类型
→ 短事务内重读当前身份授权、发布状态及事实版本
→ 一致时保存 QualificationEvaluation、命令终态、审计和 outbox
→ 返回可查询结果；事务后投递虚构通知
```

演示输入从同一租户的受权测试事实读取，使用虚构组织/数据。申请者提供目标资源引用和参数，不能直接提供“已拥有资格”的可信事实。WASM 只拿到必需字段，持久日志不记 token 或无关个人资料。

FactSnapshot 包含参与计算的行版本和查询集合 revision。所有可能改变资格集合的新增、修改、删除必须推进其 revision；对无法建立完整依赖版本的规则，P1 不接入。提交前发现权限、输入、激活版本或 trust epoch 改变，则拒绝过期结论或按有界策略重新计算。重算不重复提交逻辑命令。

P1设独立的 `QualificationFactSet(tenant_id, scope_id, fact_kind, revision)` 行，明确每类投影的唯一owner与写命令。事实增删改、集合成员变化和归属移动必须在同一事务持tenant guard及相关FactSet行锁，更新事实并递增所有受影响集合revision；空集合也有revision行，跨scope移动同时推进两端。外部同步经相同受权写命令，不允许旁路直接写表。初次读取也按统一锁序短持guard/FactSet锁，在锁内读取数据与revision形成一致快照，再提交释放；禁止先读旧数据、后读新revision后当作有效快照。相关状态如受时间窗口影响，额外记录最早失效时间，提交时核验；缺乏完整writer与时间协议的事实源暂不接入。

演示一家企业的两个部门/项目使用相同能力协议和不同受控规则包，验证不同人员、同一人员的不同任职/scope 之间的边界；不以进程内 realm 代替业务授权。模块输出即使写出 allow=true，也不能改变宿主的 authz 判定。

## 6. 最小批准、签名和激活

P1 发布对象是固定计算能力的 manifest，字段边界见[核心对象](core-object-contracts.md)。它覆盖原始模块摘要、ABI/输入输出契约、允许 imports、资源预算、tenant/environment、前置 revision、sequence、有效期及治理引用。

首次信任建立由受控安装流程完成：明确部署/环境、允许issuer与audience、RP/origin、开发发布公钥、首个管理员的已验证外部主体映射及最小授予，形成可审计BootstrapRecord并关闭初始化入口。首个登录者不能自动成为管理员，包不能自带公钥建立信任，缺初始可信配置时拒绝ready。重复初始化必须幂等且不重新开权；恢复/重置走单独受控流程。P1只用隔离开发根和虚构主体核验此链，不自行生成真实企业治理政策。

此处缺可信配置的 readiness 约束针对后续签名批准/激活能力组合。当前基础 `/ready` 表示 runtime、必需依赖及数据库 schema 就绪，不代表部署企业身份已初始化；没有登记唯一组织根、profile、来源准入或成员的身份请求由入口拒绝。P1b 首条 bootstrap 只登记虚构身份与成员，不创建管理任职或发布权。

批准动作绑定精确 manifest digest 和一次性意图；身份模块核验批准人的当前权限和认证证据。发布工具/服务只能签经过检查的内容。绑定意图的批准验证在 P1c/P1d 范围内，实施前选择 Stage RP WebAuthn 或 IdP step-up profile，不默认选定 Passkey，也不要求先建完整审批产品；普通登录或 MFA 成功不能直接记为用户已签署任意文件。

签名格式与算法必须采用固定 profile 和成熟实现，拒绝算法降级、未知 key 或跨环境 key。开发签名密钥与生产分开；开发密钥不能让产物越过生产门槛。身份凭据密钥与组织发布密钥分别管理，轮换和撤销分别记录。

Activation 采用 expected_active_revision 条件更新；加载时校验准入，开始执行和提交结果前重验当前发布/信任状态。P1 遇到当前激活已变化时不把旧结果当新规则结果提交。单个有效批准人的最小批准策略可用于测试；多责任人审批、完整组织发布工作流和 KMS 产品集成后续扩展，但不能把独立多人批准称作已经实现密码学阈值签名。

<a id="transaction-protocol"></a>
<a id="7-事务并发与撤销协议"></a>
## 7. 事务、并发与撤销协议

P1 延续部署组织持久 AuthorizationGuard，把受控授权变更、激活/撤销、资格事实 revision 变更和评估提交放进明确锁序。前置身份验证与输入投影读取尽量短；WASM 执行、签名服务和任何网络 I/O 均不在业务锁/事务内等待。

2026-10-26 并发优化已落实：已核查的授权读者使用组织共享保护，授权写者保留排他保护；实现与复测见[记录](reviews/concurrency-optimization-2026-10-26.md)，[改进计划](stage-concurrency-improvement-plan.md)保留推导与路线约束。不能把“普通元数据编辑可共享”推广为全部业务命令，新增批准／激活／资格事实写者须先纳入完整冲突矩阵。

统一目标锁序为：本次短事务需要的逻辑命令入口幂等 mutex（如适用）→ 相关信任域的 TrustState 行 → 涉及的全局主体状态行 → tenant AuthorizationGuard（入口确定 S/X）→ ApprovalIntent / challenge / approval记录 → Activation → QualificationFactSet及领域对象 → 持久 CommandExecution / 结果行。入口的逻辑键 mutex 与最后的 execution 行锁是两类对象。只取所需锁，同类别按稳定ID排序；批准、撤销、激活、事实更新、初次快照和最终提交均遵守此序，不能先拿challenge再等待guard，也不能在普通 handler 中嵌套取得另一条命令的键。权威写入与读取协议共同验收，不只在评估命令端加锁。

事务级入口 mutex 只覆盖一次短数据库事务，不能跨 WASM、签名服务、网络或人工等待持有。EvaluateQualification 的受理与最终提交分别取得必要保护；中间执行权由持久 execution/attempt 状态与条件终态维护，重启后的旧 attempt 不能覆盖新尝试。该最低恢复保证在 P1d 验收，P4 再验证跨节点租约/fencing，不能把持久执行权推迟为单纯的进程内状态。详见[并发提案路线接缝](stage-concurrency-improvement-plan.md#route-compatibility)。

TrustState记录平台受控 `trust_config_revision / key_status_revision`，key区分可签可验、停止新签但按政策可验旧签、已撤销等用途状态。首次准入、批准证据、调用记录保存其验证到的revision和认证新鲜度引用；加载、dispatch与最终提交核验当前状态。revision变化时按现行政策重新验证或拒绝陈旧结果，不能仅信manifest自报epoch，也不把正常轮换与泄露撤销混为一谈。此状态反映本地已验证的信任配置，不承诺即时观察外部IdP/KMS的一切变化。

全局主体禁用、信任根轮换等变更必须确定受影响范围并使提交路径观察到新状态；不能可靠覆盖时 P1 拒绝该类变更，不能伪称全局即时撤销。外部 IdP JWT 的撤销可见窗口独立记录，不把本地 guard 当成能立即观察外部一切事件。

人员身份读取与公开元数据命令采用共同全局锁协议：相关 TrustState 与 Principal 行按顺序持 `FOR SHARE`，再取得组织 guard，随后读取授权和资源；对应身份/信任撤销持相应 `FOR UPDATE`。`AuthenticatedAuthorizationLoader` 连接真实认证证据与当前单任职授权，命令提交前重验 profile/trust/外部映射、来源准入、成员及有效截止。网络验证不占数据库锁，公开请求不接收自报的 ExecutionContext。身份及授权历史竞争证据分别保留，新增人员命令的真实 PG 竞争与 HTTP 验收见[P1b.3 记录](reviews/p1b3-human-work-2026-10-25.md)。

后续受控提交继续采用相同锁序：锁外只发现候选依赖，锁后重载；依赖集合改变时整笔有界重试，不能持 tenant guard 再补前序锁。新增 mapping/profile/binding/member writer 必须纳入共同协议；无法建立共同锁点并覆盖唯一部署组织 guard 时拒绝变更。

组织根停用、成员/任职/关系及组织来源准入变更，与兼容 tenant epoch 推进同事务；全局主体与信任分别推进 identity epoch/revision。企业请求锁后重验部署组织状态、认证映射、来源准入和有效截止。已有技术服务认证路径继续核验其 registration，但不能作为人类命令的身份依据。身份写使用变更前事实验证操作者，随后持久变更和审计，不能直接放入禁止修改授权事实的旧业务 handler。首条只读身份入口不授予业务能力。

同一逻辑命令有唯一幂等范围，参数摘要独立保存。并行重复请求只能有一个确定提交；输入不一致则冲突。结果丢失先按原命令身份查询，禁止更换 key 后盲目重放。身份/权限变化后的结果查询与敏感错误也要校验当前读权限。

业务结果、命令终态、审计与 outbox 同事务提交。outbox 至少一次投递，稳定去重键、租约、独立 attempt 和回执状态可核对；外部接收端不支持幂等或查询时，明确保留 unknown，不承诺 exactly-once。

人员发起的延迟动作或队列工作必须保留原人员、成员经历、选定任职与原命令因果链，在实际执行与提交前按当前权限重验。Worker、runner 和 `ctx.spawn()` 任务是技术执行设施，不能从队列记录取得永久人员权限，也不能将工具辅助自动记为人工确认；需要人工确认的动作必须验证实际确认记录。具体延迟身份凭据与恢复协议必须先独立验收，不通过保存原 bearer token 实现。

2026-10-25 选型已确定为 PostgreSQL 17＋Redis 8：PostgreSQL 是唯一长期主库，MySQL 退出受支持配置与新门禁。SQLite 只做适用子集的快速检查。P1 新增身份、发布和结果模型的迁移/锁/撤销竞争须在 PostgreSQL 17 真库验证；现有迁移历史与验证 JSON 保留，不改写成当前已验收结论。

Redis 只作可重建缓存、非权威短期协调和可丢失唤醒，不接替 PostgreSQL 中的授权 guard/epoch、幂等、outbox/attempt 或业务租约。清空 Redis 后业务事实和待办可从 PostgreSQL 恢复；缓存必须隔离部署环境、组织及主体/作用域、核验当前权限版本，不能绕过撤权。PostgreSQL 与 Redis 不承诺双写原子性，每项使用须验收超时、故障与回退/拒绝策略。当前 Redis 配置与客户端工厂不作为 `main` 的强制依赖，也不代表这些业务用途已接入，细则见[存储策略](storage-strategy.md)。

## 8. 持久模型最小范围

- 人类 Principal、ExternalIdentity、OrganizationMembership（兼容物理表 `tenant_membership`）、RoleAssignment、角色版本、scope/resource/关系及 AuthorizationGuard；已实现的技术服务认证记录保留。
- 固定能力定义/实现引用、Artifact/Manifest、ApprovalIntent/Evidence、Activation、TrustState与key状态；QualificationFactSet负责资格输入集合revision。
- CommandExecution、ExecutionAttempt、FactSnapshot 引用、QualificationEvaluation、AuditEntry、OutboxMessage 与投递 attempt。

字段草案与存储权威见[核心对象](core-object-contracts.md)及身份/WASM 专项文档。测试资格事实可以是有限虚构 schema；不提前引入完整任务、承诺和容量产品模型。已有四类底板模型保留，后续迁移不得无故重置用户数据。

身份 schema 固定成员经历、角色版本、动作与资源范围、组织认证来源准入。AgentRegistration、AgentWorkloadBinding、Delegation/term 不在一期建表清单，ServiceGrant 不作为人员或 AI 辅助调用的前置依赖。迁移先增量扩展，保留旧 ID/审计；旧行没有可信组织/membership 映射时保持隔离，不补默认公司、不解析 scope 前缀，阻止全表约束收紧与历史权限全量切换。内部新 loader 只读取完整新引用，不接受未映射旧行。首次 bootstrap 与授权开发变更分别在其事务中记录配置摘要/操作键和结果，重放不重新开权。

## 9. 一期联调验收

以下全部是待执行场景；当前通过数为 0。历史 98 项底板测试单独记录，不能算入新基座验收。

| ID | 验收内容 |
|---|---|
| F1-01 | 真实测试 IdP 人类凭据→内部人员主体→本企业成员资格→正确 manifest→WASM→提交结果完整贯通；HTTP/CLI 使用同一命令契约，技术 Worker 不取得独立业务成员身份 |
| F1-02 | 同企业两部门/项目及两主体并发评估，不共享可变身份、WASM memory、句柄或敏感日志 |
| F1-03 | 输入/结果中的伪造 tenant、角色、actor、module digest 均不能改变宿主权威上下文 |
| F1-04 | 计算期间成员、任职、主体、资源关系或包被撤销，提交路径按明确竞态顺序拒绝过期结果；AI 辅助不绕过人员当前权限 |
| F1-05 | 计算期间资格集合新增/删除或对象版本变化，检测冲突；无无法说明来源的结果 |
| F1-06 | 相同命令重复提交、IPC迟到/重复回执、worker重启，不重复写评估结果与outbox |
| F1-07 | runner 在编译、start、执行、输出各阶段崩溃/超限，宿主保持响应并回收资源；失败状态准确 |
| F1-08 | provider 换代和关闭期间拒绝新旧代次混用；不能在runner仍执行时报告已排空 |
| F1-09 | 签名/摘要/环境/ABI/能力预算/前置revision不符即拒绝；未批准内容不能激活 |
| F1-10 | DB已提交但响应丢失，原幂等查询得到结果；外部通知回执丢失保留unknown并对账 |
| F1-11 | PostgreSQL 17 真库迁移、授权guard锁序与竞争、版本条件更新验证；不拿SQLite结果代替；Redis使用方另验不可用/清空/失效丢失，不丢业务事实或绕过撤权 |
| F1-12 | 生产配置拒绝fixture、开发签名根和未验证runner配置；日志/错误/readiness不泄露秘密 |

Core C1–C8、身份 I1、WASM W1、对象 O1 与本表分别记录执行环境和结果；O1-08 属 P3 未来通用流程，不混入本期完成声明。逐场景清单、精确依赖版本、命令与未覆盖平台作为验收证据保存。

## 10. 下一阶段与生产门槛

P1 完成后进入 P2 的[任务业务切片](business-slice-plan.md)、蓝图编译、语义 diff 和更完整的组织发布治理。P3 扩展自研持久流程。WASM 的通用 hostcalls、WIT Component Model、多方密码学协议、跨组织隐私证明和多节点编排分别验收，不能由本期纯计算节点推导已支持。

本地测试 IdP、开发签名根与虚构数据通过，只证明基座集成。在接入真实用户前，必须确定生产身份与账户恢复、密钥服务、隔离平台、数据保留和访问、迁移备份恢复以及运维责任；具体验收范围另行记录。
