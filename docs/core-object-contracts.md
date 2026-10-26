---
title: Stage 核心对象与契约对齐
status: design-proposal
implementation_status: p1b3-human-work-implemented-full-p1-pending
policy_effect: none
review_date: 2026-10-24
revised_date: 2026-10-25
---

# Stage 核心对象与契约对齐

当前采用企业私有化部署：一套数据库一个组织授权根和一个环境。以下 `tenant` / `TenantMembership` 等沿用内部存储命名，指服务端确定的组织根；不形成客户端选择组织的协议。集团、子公司、部门等业务组织单元以后按需建模；部门和项目使用 scope，scope 不自动等同组织树。HTTP `/api/v1` 对外使用 `organization_ref`。详见[组织授权根语义](organization-root-semantics.md)和[私有化部署与 API v1](private-deployment-api-v1.md)。

本文统一组件、身份、WASM、发布与后续流程的词汇和责任。字段是完整目标的逻辑契约；P1a、身份/成员读取、单任职数据库授权及 P1b.3 人员工作项元数据命令已分切片实现。实际 Python 类、schema、API 与验收见[P1b.3 记录](reviews/p1b3-human-work-2026-10-25.md)及其前置记录；不存在从对象名称推导整个目标已完成的约定。三层运行时与能力组合的阶段决策见[演进决策](runtime-evolution-and-composition.md)。

**一期协作边界（2026-10-25 对齐）**：采用“人员—公司—人员”。公司承载共同规则、成员与协作事实，人员持有任职、作出业务决定并承担责任；Agent 仅作为人员监督下的工具，不独立成为公司成员、任职主体或协作者，也不另设 Agent 业务接口。既有 `human / agent / service` 枚举、服务认证和历史数据保留；它们不使独立 Agent 授权成为一期需求。下列 Agent 登记、承载绑定、Delegation 与通用 ServiceGrant 仅为后置候选，不是一期实施约束，也不承诺后续阶段。详见[人员—公司—人员协作边界](people-company-collaboration.md)。

## 1. 共同约定

- 业务引用包含 `tenant_id + object_id`，保留为内部兼容引用，由宿主填入唯一部署组织并验证所属关系。主体与信任配置在部署级作用域，不能把其 ID 当作任意资源权限。
- 标识与版本分开：稳定 ID 标识对象；revision 表示可变对象的乐观版本；digest 标识不可变字节；generation 表示组件一次激活。四者不得互相代替。
- 定义使用稳定 ID 与 schema/接口版本，运行记录使用独立 ID 与因果引用。显示名称、邮箱、角色名称和画布坐标都不是安全标识。
- 外部输入是请求选择项。一期身份、部署组织归属、当前成员、选定任职、资源属性、信任根与当前授权由服务端构造或从受信任来源验证；Agent 生成内容不能决定身份、授权或人工确认。
- 所有成功声明说明成功边界：计算返回、宿主验证、数据库提交、外部确认分别表达。时间统一保存 UTC，展示时另用用户时区。
- P1 拒绝无法解释的额外字段、重复键、未登记版本与无限资源预算。确切序列化、最大长度和算法由各子规格冻结；“有一个 dict”不代表已有稳定协议。

运行时对象与业务对象分层。`ComponentContext` 是后端组件的一次激活上下文，包含服务绑定、realm、资源 owner、generation、后台任务和关闭操作；它不是 Agent 会话、HTTP 请求、`ExecutionContext`、`AuthorizationContext` 或数据库事务。`owner_id` 和 `generation` 只保护进程内资源生命周期，不能被当作主体、组织、业务版本或授权 epoch。`ExecutionContext` 每次执行重新构造，`UnitOfWork` 只覆盖当前数据库事务，未来 `WorkflowRun` 则通过持久记录恢复；三者均不写回共享 `ComponentContext`。

Stage 的三层运行边界是：Component Runtime 管 provider、连接器、WASM runner 与 Worker 的进程内生命周期；Execution Kernel 管身份、授权、命令、幂等、审计、outbox 和外部回执；Workflow Runtime（P3）管持久 run/step/attempt/wait/recovery。Core 的服务租约不等于业务权限，disposer 不会回滚已提交事实，进程内事件不替代 outbox。

## 2. 五组核心对象

### 2.1 能力定义与运行绑定

本表中 CapabilitySpec/CapabilityBinding 是 P2 通用组合的目标对象，尚无相应运行时类或独立注册表。当前 CommandKernel 只接受显式传入的固定 CommandSpec 集合，其在途调用由外围服务组件租约保护；ServiceKey 与 BindingIdentity 已在 Core 实现。

| 对象 | 关键字段 | 权威与生命周期 |
|---|---|---|
| `CapabilitySpec` | capability_id、interface_version、input/output_schema_digest、required_action、effect_kind、implementation_digest、resource_profile | 平台审核的不可变能力定义；声明执行什么，不授予调用权 |
| `CapabilityBinding` | spec_digest、handler_ref、provider_identity、generation、registration_id、realm、availability | 当前能力绑定；应用层注册借用 Core owner/登记身份，激活后可租用，退役时禁止新调用 |
| `ServiceKey` | namespace、name、interface_version、value_contract | 组件依赖标识；可能提供数据库工厂等内部服务，不等同可授权业务命令 |
| `PermissionAction` | action_id、resource_kind、scope_rule | 授权策略的稳定能力点；由可信授权模块解释 |

命名避免歧义：某个 Python provider 提供服务对象、某个业务能力已注册、某个主体获准执行动作，是三个独立事实。realm 控制服务解析，兼容字段 tenant 表示部署组织，scope 表示企业内授权范围，三者不可互换。

2026-10-25 的[框架实施规格](framework-foundation.md)进一步规定：Core 的 ServiceLease/CallScope 不含身份，由应用 ExecutionContext 组合持有；服务与监听登记另有 registration_id，旧句柄不得影响同 owner/generation 下的新登记。未来命令贡献登记和业务 schema 留在应用层，不进入通用 Core；当前固定 CommandSpec 尚无独立登记句柄。

`effect_kind` 草案至少区分 `pure`、`query`、`transactional_command`、`external_delivery`。重试规则、幂等和执行边界与类别关联；能力作者不能通过改标签绕过宿主检查。P1 WASM 仅承载 `pure` 计算，由固定可信 handler 组织输入与提交。

### 2.2 执行身份与上下文

| 对象 | 内容 | 边界 |
|---|---|---|
| `Principal / ExternalIdentity` | 内部主体；受信任 issuer/subject 与主体的已验证关联 | 凭据身份与真实自然人关系不是同一保证；多账号关联需单独证据 |
| `TenantMembership / RoleAssignment` | 一次组织根内成员经历；固定该经历、角色版本和 scope 的具体任职 | 同根/principal 最多一条未终结成员经历，重入使用新 ref；新角色版本不自动迁移旧任职；蓝图岗位不自动生成任职 |
| `AuthenticationProfile / TenantAuthenticationBinding` | 明确凭据验证语义；企业接受的 profile/version、环境及 client/assurance 条件 | 账号关联不扩大企业接受来源；同 provider/namespace 不跨版本重解释主体类型和映射语义 |
| `WorkloadRegistration` | 服务主体、受信任认证来源、client、环境、状态与版本 | 已实现的技术服务认证；不自动成为协作者或获得人类业务权限 |
| `AuthenticationEvidence` | principal_ref、凭据类型、issuer、audience、认证时间/强度、到期、验证来源 | 记录已验证认证证据，不携带业务 allow 结论；持久记录不保存原始 bearer token |
| `ExecutionContext` | execution_id、已认证人员 actor、组织根、当前 membership、选定 assignment、causation、deadline、plan/manifest_refs | 一期人员业务命令由宿主构造；当前兼容类型中的 delegation/grant 不构成一期需求，runner 只获得必要子集 |
| `AuthorizationContext / Decision` | 当前事实、动作、对象版本、关系、策略/撤销 epoch；allow/deny 与原因码 | 当前事务/动作内有效；历史 allow 不是可重放的授权令牌 |

具体映射、凭据验证、撤销和 Passkey 规则见[身份与授权](identity-and-authorization.md)。已发布计划固定业务规则，运行时安全策略仍可收紧；当前撤销或平台禁用优先，不能靠旧包继续获取执行权。

人员通过可信客户端/宿主发起共同的命令，Agent 可以辅助准备输入。用户凭据不交给模型、Agent 沙箱或 WASM；宿主根据真实认证、当前成员与明确任职授权。审计分别表达业务责任人员、技术执行来源和确有证据的人工确认，不能将“自动化辅助”记成“人员已批准”。既有 outbox Worker 执行已提交的投递意图，不需要因此构造一个 Agent 成员；新增后台业务任务须另行定义受限执行与当前重验合同。

以下仅保留**后置候选，非一期实施约束**；是否采用、何时采用，须由新的真实需求重新评审：

| 候选对象 | 待评估内容 | 当前边界 |
|---|---|---|
| `AgentRegistration / AgentWorkloadBinding` | 独立 Agent 的登记经历与实际 workload 绑定 | 不建表、不要求一期接口携带 agent_ref、不设一期验收门槛 |
| `Delegation` | 独立代理主体的有限 action-target 委托 | 人员使用工具不自动产生 Delegation；不由此建设代理业务通道 |
| `ServiceGrant` | 后台技术任务的明确服务权限 | 已有服务认证继续保留；真实后台需求出现时再决定所需授权合同，不与 Agent 捆绑 |

持久字段、复合组织根外键、类型约束与角色版本见[身份表结构](identity-schema-design.md)。外部身份/成员/任职、资源 ACL 与领域关系各有唯一权威，ABAC 从可信领域字段读取。成员重入不得恢复历史授权；未映射旧行阻止全表 NOT NULL 收紧和历史权限全量切换，内部新 loader 只接受完整新引用。不从旧 scope 字符串猜测组织归属。

平台初始化与身份变更分别保存 `BootstrapRecord` 和 `IdentityControlOperation`：配置摘要及初始授权、平台操作的幂等范围及受限证据/结果同事务保存，不使用假造 tenant。首次身份只读切片不产生业务执行或授予角色。

### 2.3 命令、执行与结果

| 对象 | 关键字段 | 用途 |
|---|---|---|
| `CommandEnvelope` | command_type/version、tenant、target_ref、expected_version、idempotency_key、payload、payload_digest | 调用方的受检请求；身份和部署组织由入口另行构造，公开 API 不接受 tenant 选择 |
| `CommandExecution` | execution_id、已认证 actor、命令类型、幂等范围、输入摘要、计划摘要、状态、结果引用 | 稳定的一次逻辑业务动作及可查询结果 |
| `ExecutionAttempt` | attempt_id、execution_id、worker/runner_identity、lease/fence、开始/结束、预算、错误 | 同一动作的具体尝试；重试新建 attempt，保留业务幂等身份 |
| `FactSnapshot` | 受限输入、事实版本向量、QualificationFactSet revisions、查询/归一化版本、输入摘要、最早失效时间 | 在统一guard/revision锁下采样数据与版本；覆盖集合新增/删除和时间失效，不只记录已有行版本 |
| `ComputationReceipt` | invocation/attempt、module/ABI/input_digest、输出摘要、预算与退出原因 | WasmRunner 的计算回执；不是数据库提交、授权正确性或密码学执行证明 |
| `CommandResult` | status、stable_error_code、业务结果/版本、result_ref、effect_status | 经过宿主验证的持久结果；按当前读权限查询 |
| `OutboxMessage / DeliveryAttempt` | 稳定消息与去重键、读者范围、payload_ref、接收端、租约、回执/unknown | 事务提交后的外部投递与对账 |

对同一逻辑命令使用 `(tenant, authenticated_actor, versioned_command_type, idempotency_key)` 唯一范围。输入摘要单独存储，参数不同而 key 相同应冲突。重复命令需先验证当前身份及结果读权，再返回已有结果；不得重新执行已提交副作用。关联旧授权和当前策略的处理见[一期事务协议](phase-1.md#transaction-protocol)。

命令状态草案：`accepted → running → succeeded / failed`；等待批准可用 `needs_approval`，但 P1 不实现通用审批流。无法确定外部执行结果时使用 `pending / unknown` 并对账；不能把已明确提交的业务命令因通知未送达改成 failed。`cancel_requested` 是请求，只有确认停止且结果已核对才能标记 cancelled。API 响应丢失时，用稳定命令身份读取结果。

### 2.4 流程定义与实例

| 对象 | 关键字段 | 责任 |
|---|---|---|
| `WorkflowDefinition` | definition_id、revision/digest、node/edge_specs、引用能力与版本、状态格式版本 | 冻结业务图；P1 只定义边界，不交付通用解释器 |
| `WorkflowRun` | run_id、tenant、definition/plan_digest、发起者/授权引用、状态与版本、结果 | 持久实例；不能存成 Fiber 的临时内存状态 |
| `StepExecution` | run/frame/node/iteration identity、command_execution_ref、输入/结果引用 | 固定逻辑步骤；每轮循环是不同步骤，网络重试属于同一步 |
| `WaitRecord` | wait_id、事件/人工动作、deadline、correlation、expected_state_version | 等待与恢复；不持有数据库事务和 provider 租约 |
| `WorkflowCheckpoint` | snapshot_schema、定义摘要、队列/分支/步骤状态、最后处理事件与版本 | 恢复依据，需迁移规则与可追溯输入；快照写完不自动证明副作用一致 |

流程引擎获取短期能力租约执行步骤，结束后释放；等待期间只保留定义和实现引用。未完成实例引用的包不可回收；找不到旧版本应阻断并报告，不静默换 latest。当前权限仍按每次动作重验。定义迁移需节点映射、待办/计时处理、未确认效果对账与独立批准。

同库时步骤完成/恢复唤醒与业务结果原子提交；跨存储时使用业务 outbox、幂等消费和 CommandExecution 查询推进。命令已成功而步骤尚未确认完成时，恢复应取得原结果，不能重复产生副作用。

### 2.5 发布、批准与激活

| 对象 | 关键字段 | 责任 |
|---|---|---|
| `BlueprintSource` | source_revision、业务定义、组织选择器、布局/可读说明 | 编辑素材；发布前区分语义与显示字段 |
| `CompiledPlan` | source_digest、compiler_version、IR_schema、展开配置、固定依赖、能力上限 | 确定执行输入；编译器是信任边界的一部分 |
| `ReleaseManifest` | manifest_schema、包/计划/模块摘要、ABI、tenant/env、能力/imports、资源预算、前置revision、sequence、期限、governance_policy_ref | 被批准和签署的确切发布内容；P1 使用小型计算模块清单，不要求完整蓝图编辑器 |
| `ApprovalIntent / ApprovalEvidence` | manifest_digest、动作、tenant/env、nonce、期限、批准人/任职、认证断言/新鲜度、trust_config_revision/key_status_revision与核验记录 | 将高风险确认绑定不可变内容；授权批准与签名格式分开 |
| `ReleaseSignature` | manifest_digest、受允许算法、key_id、签名与验证结果 | 证明被信任密钥对确切内容签署；不证明业务正确或当前仍有授权 |
| `Activation` | tenant/env、manifest_digest、expected_active_revision、sequence、目标/批准/实际revision、验证时trust/key状态revision、结果、撤销状态 | 决定当前获准加载什么；提交前核验当前信任状态，条件更新防止旧计划覆盖新状态 |

P1 的 manifest 可直接覆盖固定 WASM 能力而不经过通用编译器；P2 才让完整 BlueprintSource 编译为计划。所有引用必须固定到内容摘要，显示版本号或 URI 不是制品身份。

编码统一为：构建阶段按冻结的encoding profile生成并保存唯一manifest bytes，ApprovalIntent引用该bytes的digest，ReleaseSignature原样签验。验签时不重排或再序列化；执行对象从同一已验证bytes解析。TrustState的权威revision来自宿主，不能由包自报替代；正常轮换、停止新签和密钥撤销分别有政策。

专项规格中的 `WasmReleaseManifest` 是此处 ReleaseManifest 的受限计算模块 profile；`ReleaseEnvelope` 是 manifest 与 ReleaseSignature 的传输封装；`AdmissionDecision / ActivationReceipt` 分别是当前准入判断与 Activation 的观测结果，不另造相互竞争的事实源。

签名包与管理员凭据使用不同信任配置：批准人证明自己批准了这次意图，发布服务核验当前权限与审批记录后签清单，执行器再校验包、信任/撤销状态与本次业务授权。信任根或批准规则修改使用当前有效的独立治理协议，新包不能批准自己。

第一份信任配置和管理授予由受控部署初始化产生BootstrapRecord，不能用“首个用户”或包自签完成。之后的变更由当前生效规则控制，初始化入口保持关闭；开发与生产环境分别建立根。

撤销区分停止新激活、停止新执行、终止在途计算三种动作；终止在途计算不能撤回已有提交。回退发布使用新的 sequence 引用旧制品，同时检查旧依赖/数据兼容性，不回拨发布序号或恢复已撤销权限。

## 3. P1 对象贯通示例

P1 的 `EvaluateQualification` 是基座演示，不授予真实岗位或业务权限：

1. 人员通过可信客户端使用真实协议进入 Stage，宿主加载该人员的当前成员与选定任职；Agent 辅助输入不改变调用身份，也不代表已经人工确认。
2. 校验评估能力、输入读取权和已激活的签名模块；创建稳定 CommandExecution 与 attempt。
3. 按一期锁序短持相关guard/FactSet锁，一致读取最小FactSnapshot及revision后关闭事务；在租约和预算内送入独立WasmRunner。
4. runner 返回带 invocation、attempt、模块/输入摘要的结构化计算回执；宿主验大小、类型、关联和状态。
5. 新开短事务，按锁顺序重验当前权限、发布/信任撤销状态与事实 revision；变化则拒绝提交过期结论或按有界策略重新计算。
6. 同事务存评估结果、命令终态、审计和 outbox；返回可查询结果。投递由测试 sink 证明去重与 unknown 对账。

计算期间不持有业务锁。QualificationFactSet的owner与全量writer协议以[一期事务方案](phase-1.md#transaction-protocol)为准；空集合、增删改、scope移动、时间失效都必须可观察。没有完整revision协议的规则P1不接入。规则输出是业务候选，最终授权不能交给上传模块决定。

## 4. 失败与升级的共同规则

| 情况 | 必须表达的结果 |
|---|---|
| provider 缺失 / 切换 | 拒绝新租约，按能力状态等待或返回 unavailable；旧代次清理有完整结果 |
| 认证失败 / 授权撤销 | 在入口或提交前拒绝；原审计事实保留，敏感结果读取仍受限 |
| 包摘要错误 / 签名无效 / 版本未登记 | 在执行前拒绝，不能降级到另一个实现 |
| WASM 超限 / runner 崩溃 | 记录失败原因与 attempt，拒绝迟到回执；只有纯计算才能有界重试 |
| 计算完成但事实变化 | 丢弃过期候选，返回稳定冲突原因或开始新 attempt |
| DB 提交回执丢失 | 根据原 execution/幂等身份查询，不换 key 重做业务 |
| 外部已执行但回执丢失 | 保留 unknown，通过接收端幂等与查询对账 |

框架、接口、能力实现、蓝图、实例定义、快照和密码学算法分别版本化。升级承诺需兼容矩阵与故障证据；不由签名或标准名称自动产生向后兼容保证。

## 5. 待冻结事项与验收

本轮先对齐对象语义；公开 schema、数值预算、错误码枚举与具体协议参数仍需评审。内部工程规范不称官方 Cordis 标准、行业认证或已完成 SGES；现阶段沿用小范围工作规格标识。

| 场景 ID | 待实现验收 |
|---|---|
| O1-01 | 同一能力定义的编译引用、注册 handler、manifest 和执行回执一致；版本/摘要不符拒绝 |
| O1-02 | 同主体的两个任职/scope 并发请求互不串用上下文或数据 |
| O1-03 | 同 key 同输入返回已提交结果；同 key 不同输入冲突；attempt 变化不更换业务幂等身份 |
| O1-04 | 计划固定旧版本，provider 换代不混用绑定；当前撤销仍阻止后续动作 |
| O1-05 | 规则计算期间相关行或集合变化，提交前检测到；不能存过期结论 |
| O1-06 | 提交成功但回执丢失后重连，结果可核对且审计/outbox 不重复 |
| O1-07 | manifest 修改任何执行语义后旧批准不可复用；旧前置revision不能覆盖新激活 |
| O1-08 | 业务提交与流程完成间故障通过原命令结果恢复；用于 P3 流程验收，不计入 P1 已交付通用引擎 |

上述场景全部未执行、未通过；详尽身份与 WASM 场景分别见[身份规范](identity-and-authorization.md)和[WASM 宿主](wasm-host-design.md)。Core 的原 34 项另行统计，不与本表相加宣传为已实现功能。
