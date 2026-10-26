---
title: Stage 运行时演进与能力组合决策
status: design-direction
implementation_status: component-core-assignments-login-mfa-server-implemented-client-wasm-pending
policy_effect: none
decision_date: 2026-10-25
---

# Stage 运行时演进与能力组合决策

本文记录本轮对 Stage 自研运行时定位的复盘结论。它是后续运行时、能力组合和流程设计的入口；不改写历史研究记录，也不把尚未实现的目标写成当前能力。

## 1. 先确定目标

Stage 是企业私有化部署的后端服务。它需要在身份撤销、连接器超时、Worker 重启、组件换代、数据库回执丢失和外部结果未知时，仍然保持责任归属、授权边界、幂等、审计、版本和可恢复性。

业务主体边界固定为早期**人员—公司—人员**模式：人员持有成员资格和任职，并对协作动作负责；Agent 是人员监督使用的工具，不单独入职、承诺或验收，不单设业务接口。Component Runtime 管理 Worker、runner 或工具适配器，并不因此引入 Agent 业务主体。独立 Agent 身份/绑定/Delegation 仅保留长期候选且无默认排期；ServiceGrant 按真实技术后台任务另案设计。详见[人员协作边界](people-company-collaboration.md)。

因此，Cordis 与 Dify 的作用是**机制研究参考**：

- Cordis 帮助研究组件依赖、资源 owner、代次、调用租约、停止和排空。
- Dify 帮助研究图定义、节点目录、发布版本、运行实例和暂停恢复。
- 两者都不是 Stage 的兼容目标、生产依赖或现成执行引擎。

Stage 不以“API 长得像 Cordis”或“能够直接运行 Dify DSL”作为完成标准。某项机制只有在帮助 Stage 保证后端状态、外部副作用或恢复语义时才进入实现范围。

## 2. `ComponentContext` 的准确边界

`ComponentContext` 是**后端组件运行上下文**。它表示某个组件的一次激活代次可以解析哪些服务、登记哪些资源、启动哪些进程内任务，以及如何停止和收尾。

它不表示：

- Agent 会话、模型上下文或工具调用计划；
- HTTP request、CLI 请求或消息消费上下文；
- 当前登录主体、成员经历、委托或授权结论；
- 数据库 Session、Unit of Work 或事务；
- 持久 Workflow 的 run、step、wait 或恢复状态。

`owner_id` 只表示进程内资源归属，`generation` 只表示组件激活代次，`registration_id` 只表示一次登记。它们都不是 Principal、组织、业务对象版本或授权 epoch。`ExecutionContext` 每次执行重新构造，`AuthorizationContext` 每次判定重新加载可信事实，事务由 `UnitOfWork` 明确管理。

如果组件需要处理身份或业务数据，应接收应用层显式依赖；Core 不通过 ContextVar、全局变量或隐式“当前用户”提供这些事实。

### 2.1 插件化的边界与衡量方式

Stage 采用稳定执行协议之上的受控功能组合，不以插件数量或“万物皆插件”为验收目标。受信任业务模块可以提供服务、命令、查询及领域规则实现；模块内部的纯函数、校验器、DTO 和数据记录不必各自登记为 Runtime 插件。身份、授权、数据库适配器也可以按组件机制装配，但改变平台信任和强制执行协议必须经过独立受控变更，不能由普通业务模块给自己开权。

插件表示实现和运行资源的组合边界。角色、成员、业务对象、幂等结果和审计仍是持久事实；插件停用不撤销这些事实。Python 插件属于可信代码，Core 的依赖声明、realm、资源归属和清理机制不构成对恶意 Python 的沙箱。

新增功能先判断它是否需要独立依赖、资源、启停或替换。只增加业务计算或校验时，可加入现有领域模块和版本化命令；新增外部客户端、独立 Worker 或 runner 时，应定义受管组件。业务模块也可以整体封装成插件，前提是入口仍遵循当前身份、授权、版本、幂等及事务协议。这样保留内置模块与扩展模块采用共同装配机制的方向，同时避免无意义的细粒度注册。

## 3. 三层运行时

目标设计要求三层共享能力定义、版本绑定、执行上下文引用和结果契约，但不共享状态所有权。当前只有 Component Runtime、固定命令集合及 Execution Kernel 已实现；通用 Capability Catalog、CapabilityBinding 和 Workflow Runtime 尚未建设，不能从这张目标表推导已存在注册平台。

| 层 | 负责什么 | 权威状态 | 典型恢复方式 |
|---|---|---|---|
| **Component Runtime** | provider、连接器、WASM runner、Worker 的依赖、资源 owner、generation、lease、readiness、停止和排空 | 进程内生命周期状态 | 重启后重新装配；旧代次拒绝新接入 |
| **Execution Kernel** | 身份、授权、命令、Unit of Work、对象版本、幂等、审计、outbox、外部 attempt 和回执 | PostgreSQL 业务事实与执行记录 | 通过原命令/attempt/outbox 身份查询、对账和重试 |
| **Workflow Runtime** | P3 的 run、step、attempt、分支、人工等待、定时、恢复和迁移 | PostgreSQL 持久流程实例 | 从 checkpoint/step 状态恢复，固定原定义和能力版本 |

Component Runtime 的 disposer 不会回滚已经提交的业务事实。Execution Kernel 的提交也不会自动证明外部系统已收到消息。Workflow Runtime 不长期持有数据库事务或 provider 租约。

## 4. 当前基线与 P1 目标

当前 P1 是可靠能力执行的基座。Runtime Core、PG/Redis provider、身份、单任职数据库授权及内部命令/投递底座已有独立实施记录。P1b.3 进一步以真实人员认证接通已有 WorkItem 元数据读写与结果恢复，见[本轮记录](reviews/p1b3-human-work-2026-10-25.md)。用户批准的最小编辑不包含任务领取、交付、验收、持久流程或能力组合 DSL；后续先按 P1b.4 核对实际人员客户端和剩余证据，再推进 WASM、签名与资格评估。ServiceGrant 和独立 Agent 委托不作为前置条件。

P1b.4 已补受保护的本人任职发现与组织统一 MFA 策略，用户选定密码＋TOTP，隔离真实 IdP 和 Stage 链路分别留证。原生模块随后补齐服务端因素绑定/恢复；剩余范围是 Maestro 桌面登录/任职选择、恢复交互及生产接入验收；不能用脚本通过代替桌面交互。P1c/P1d 的具体业务意图批准另行选定 ceremony，登录 MFA 不自动产生业务批准。人员资料、正式入离职/权限管理仍是产品缺口，按最小需求分别落地，不一次扩建完整 IAM。

当前应用模型为 **37 张表**（head `c6f3a19d820b`），主要分为：

| 分组 | 数量 | 当前含义 |
|---|---:|---|
| 身份与组织基础 | 12 | Principal、认证来源、组织根、成员经历、组织人员认证策略、workload、bootstrap 和身份操作 |
| 执行与投递 | 5 | guard、CommandExecution、审计、outbox 和投递 attempt |
| 人类授权 | 8 | 六张 action/角色版本/scope/resource 新表，以及增量扩展的 assignment/tuple；未映射历史行不进入新授权 |
| 业务示范 | 1 | 最小 work item |
| [原生认证](native-authentication-design.md) | 11 | 账号/客户端、认证请求、RP/浏览器会话、同意、退出请求、code/refresh、事件与限流 |

这 37 张表分别对应凭据与会话、身份、内部人类授权与执行底座的事实边界，不是完整目标模型的预先实现。P1 不为了“未来可能需要”一次性增加工作流、蓝图、容量、组织单元和所有连接器表；新增表必须对应已批准的能力和可验证的不变量。

P1 的可靠能力执行路径是：

```text
认证与当前授权
→ 短事务读取最小事实快照、版本和能力绑定
→ 事务外运行受控 WASM 或纯能力计算
→ 提交前重验身份、撤销、发布和事实版本
→ 同事务写入结果、CommandExecution、审计和 outbox
→ Durable Worker 在事务外调用连接器
→ 保存确定回执或 unknown，并可按原身份对账
```

计算返回、宿主验证、数据库提交、外部确认是四个不同成功边界。任何一层不确定，都不能用内存清理、Redis 命中或换一个幂等键假装成功。

后续人员发起的延迟动作须保存原人员、成员经历、选定任职、命令及动作/目标引用，在实际执行与提交前重验；受监督工具执行不等于人工批准。已有 service/workload 认证只证明技术调用来源，不能代替人员身份或继承其所有任职。技术 Worker 不需要先成为独立 Agent 成员；若技术后台动作需要自身授权，则单独评审最小服务授权与撤销合同。

### 4.1 当前装配可以做到什么

| 机制 | 代码现状 | 使用限制 |
|---|---|---|
| 服务组件装配 | `PluginSpec.requires/provides`、ServiceKey、owner/generation、调用租约与清理 | 受信任 Python 显式装配；配置内容仍由组合入口/组件校验 |
| 领域命令 | 构造 CommandKernel 时传入固定 CommandSpec 集合 | 没有独立命令登记句柄、通用 CapabilityBinding 或动态命令贡献目录 |
| 人员业务入口 | HumanWorkService 组合认证、loader、coordinator、identity_validator | 通用 command_plugin 仍是可信内部适配器，不能只安装它就声称接通了公开人员鉴权 |
| HTTP 与就绪 | FastAPI 静态注册路由，identity/human-work 当前均为默认必需组件 | 关闭 human-work 后 URL 不会消失、整体 ready 会失败；尚无配置化“任意关闭业务而其余产品正常”的交付合同 |
| restart/replace | 默认停止旧接入、排空和清理，再启动新代次 | 独立实例受控挂载可保留旧调用 pin，但不等于默认支持双代并行或零停机热更新 |

代码组织为插件、Core 支持程序化启停、产品支持按配置安装/停用，是三个不同成熟度。当前达到前两项的受控范围，第三项需要补模块依赖、路由与就绪策略、版本和历史结果协议。详见[当前代码架构](current-code-architecture.md)。

## 5. P2 的能力组合模型

P1 已可用可信 Python 组件组织业务模块。P2 在实际业务切片证明需要后，增加有限、可审阅的声明式组合与发布模型；不要求先完成通用蓝图平台才能开发业务。先选一条人员任务闭环，再用实际替换/配置需求检验组合对象的价值；AI 辅助与手动操作共用人员业务契约。目标对象如下：

| 对象 | 责任 | 不承担的责任 |
|---|---|---|
| `CapabilitySpec` | 稳定能力 ID、接口/输入输出 schema、所需 action、effect 类型、预算和实现引用 | 不授予调用者权限；不接受任意 Python 代码 |
| `Release`（由 `ReleaseManifest` 表达） | 精确能力/模块摘要、依赖版本、环境、前置 revision、批准与签名 | 不代表已经激活，也不绕过当前授权 |
| `CompositionPlan` | 将能力、配置和依赖解析成确定计划，记录版本、摘要和资源上限 | 不直接修改运行时，也不解释自然语言或任意 DSL |
| `Activation` | 在目标环境按 expected revision 激活计划，记录 observed revision、结果和撤销状态 | 不回滚已提交业务事实，不把半成功报告为整体成功 |
| `ExecutionSnapshot`（P1 可由 `FactSnapshot` 与认证证据组合承载） | 一次执行使用的能力绑定、身份证据、授权/撤销 epoch、事实版本和输入摘要 | 不是长期允许令牌，不替代提交前重验 |

未来的 `CapabilityBinding` 连接应用注册和 Component Runtime 的 generation/registration；它只表示当前 handler/provider 可以被租用。当前固定命令集合由外围服务组件和调用租约保护，没有独立能力登记句柄。能力组合和运行时装配是两个动作：前者生成确定计划，后者在受信任 composition root 中按计划装配组件。

组合计划可以引用固定的 WASM manifest、可信 Python handler 或受控连接器 adapter，但每种 effect 都必须声明外部副作用、幂等和恢复语义。计划不能改变 Stage 的信任根、认证规则、授权判断或数据库锁协议。

### 5.1 新增与停用功能的开发协议

新增功能先确定领域事实、稳定 action、输入输出、版本与副作用，再决定是否需要独立组件。当前阶段通过受控组合代码注册服务及固定命令集合，静态加入路由/OpenAPI，单独执行迁移，验证实际认证/授权、失败恢复与停止清理；不把尚未接通人员身份 validator 的可信内部 command_plugin 当成人员 API 模板，也不在插件 setup 中自动迁移或授予角色。P2 的 manifest、preflight 和 Activation 是后续加强，不能写成当前步骤已自动完成。

停用、卸载和删除数据分别设计：先阻止新的业务提交/Worker 领取，列出依赖与外部 unknown；保留当前授权下必要的结果读取与对账；排空在途调用再释放资源。既有操作必须有实际可用的只读恢复实现或明确阻止本次移除，仅保留一条静态 URL 不够。已提交对象、审计、Outbox 和版本引用不随 disposer 删除；破坏性 schema 变更、数据归档/删除属于独立迁移及保留政策。

当前没有统一的功能移除控制面。移除默认必需组件还需在部署组合中同步调整 required 集合、入口可用性和接口弃用规则，并通过回归后部署；需要改变 API 行为时按 `/api/v1` 兼容合同处理。多模块部分激活、版本依赖与历史定义保留，在真实组合需求出现时按 P2/P3 验收，不能由 PluginHandle.close 的成功推导整个业务功能已安全退出。

## 6. P3–P5 的演进

| 阶段 | 运行时扩展 | 进入条件与验收 |
|---|---|---|
| **P3** | 持久 Workflow Runtime：run/step/attempt、分支/汇合、循环、人工等待、定时、恢复和迁移 | P2 的版本、授权和发布契约稳定；验证重启恢复、重复恢复、撤销竞争、外部 unknown、旧定义保留和人工等待不占事务/租约 |
| **P4** | 多进程、多节点、持久协调、fencing、版本化 worker、desired/observed revision 和滚动升级 | 有容量或故障证据；验证节点失联、旧 worker 回写、重复投递、混合版本、租约过期和备份恢复 |
| **P5** | 受控 WASM hostcall、WIT/Component Model、扩展 SDK、有限 HMR 或隐私互操作 | 存在真实消费者、维护资源和威胁模型；每种宿主能力、扩展权限和协议独立验收 |

单进程 Core 不能被宣称为分布式协调器。P1c 计划验证无 imports 的有限纯计算与 runner 进程隔离，当前尚未实现；不能将这项有限隔离推导成通用 hostcall 沙箱或密码学隐私证明，P5 扩展时另行验收。

## 7. 五组必须保持的区别

| 容易混淆的对象 | 正确区别 |
|---|---|
| `ctx.spawn()` / Durable Worker | 前者是可随进程重建的进程内监督任务；后者从 PostgreSQL outbox/attempt 领取持久工作，具有 lease、fencing、attempt 和恢复 |
| EventBus / Outbox | 前者是进程内生命周期或观测通知；后者是业务提交同事务保存、可扫描和可对账的持久消息 |
| Redis / PostgreSQL | Redis 只作可重建缓存、短期协调或可丢失唤醒；授权、幂等、outbox、租约、业务事实和 unknown 必须在 PostgreSQL |
| Runtime cleanup / 业务回滚 | cleanup 只释放资源、停止任务和排空调用；已提交业务事实、审计或外部副作用必须通过新的受权命令改变 |
| 能力组合 / 任意 Python 热载 | CompositionPlan 只解析已登记、版本化和受控的能力；不开放任意 Python import、在线源码 HMR 或插件市场 |

连接器的最小边界应包括稳定幂等键、deadline、取消策略、`invoke`、结果查询和 `close`。外部超时记录 `unknown`；Worker 重启后先按原 attempt/幂等身份查询，再决定重试。关闭顺序固定为停止新领取、排空在途调用、保存未决状态、释放客户端。

## 8. 偏离检测清单

在评审代码、表结构或新组件时，出现以下任一项，就应暂停扩展并回到本决策复核：

1. `ComponentContext`、ContextVar 或全局对象开始保存当前用户、Agent 会话、租户选择、授权结果或数据库 Session。
2. 进程内 Task、EventBus 或 Redis 队列成为唯一的业务任务、outbox 或恢复来源。
3. 连接器在数据库事务中等待网络，或超时直接被转换为确定失败/成功。
4. Worker 重试更换业务幂等键，或者没有 attempt、回执查询和 fencing 记录。
5. 组件卸载、WASM 返回 `allow` 或缓存命中可以绕过提交前授权和对象版本重验。
6. 新版本 provider、handler、能力或流程静默替换仍在运行实例使用的旧版本。
7. 新表只是为未来通用工作流或多租户预留，没有当前能力、权威来源和可验证约束。
8. 通过“兼容 Cordis/Dify”作为实现理由，却不能指出 Stage 的后端故障场景和验收证据。
9. 将人员监督使用的工具建成一期独立 Agent 成员、任职、承诺者或验收者，或者为 AI 辅助操作另设业务 API。
10. 把 Agent binding、Delegation 或 ServiceGrant 作为人员执行的通用前置条件，或把 Worker/service token 直接解释成人员权限。
11. 人员发起的延迟动作丢失原人员与任职因果链、跳过当前授权，或者把模型输出/工具完成自动记为人工确认。

## 9. 短期禁止事项

P1 与 P2 早期明确禁止：

- 把 Cordis、Dify、Graphon 或其运行时作为 Stage 生产依赖；
- 直接采用 Dify DSL、Graphon 调度器或兼容层作为 Stage 流程协议；
- 实现任意 Python 插件上传、在线源码执行、通用 HMR 或插件市场；
- 把 ComponentContext 扩展成 Agent/request/identity/workflow 的万能上下文；
- 用 Redis lock、TTL 或内存队列代替 PostgreSQL 权威租约、outbox 和 fencing；
- 在没有真实消费者、容量和故障证据前拆多节点、微服务或通用 workflow engine；
- 因为目标架构包含对象就提前创建完整工作流、容量、组织和跨企业 schema。
- 在没有新的明确产品决策前实现独立 Agent 成员/任职/承诺/验收、Agent 专属业务 API，或把 Agent 身份平台排入固定后续切片。

相关文档应引用本文作为运行时演进决策入口；历史 review 继续保留其原始范围，不因本决策重写。
