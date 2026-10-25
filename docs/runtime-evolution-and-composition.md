---
title: Stage 运行时演进与能力组合决策
status: design-direction
implementation_status: p1a-implemented-p1b-first-slice-implemented-p1c-p2-pending
policy_effect: none
decision_date: 2026-10-25
---

# Stage 运行时演进与能力组合决策

本文记录本轮对 Stage 自研运行时定位的复盘结论。它是后续运行时、能力组合和流程设计的入口；不改写历史研究记录，也不把尚未实现的目标写成当前能力。

## 1. 先确定目标

Stage 是企业私有化部署的后端服务。它需要在身份撤销、连接器超时、Worker 重启、组件换代、数据库回执丢失和外部结果未知时，仍然保持责任归属、授权边界、幂等、审计、版本和可恢复性。

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

## 3. 三层运行时

三层共享能力目录、版本绑定、执行上下文引用和结果契约，但不共享状态所有权。

| 层 | 负责什么 | 权威状态 | 典型恢复方式 |
|---|---|---|---|
| **Component Runtime** | provider、连接器、WASM runner、Worker 的依赖、资源 owner、generation、lease、readiness、停止和排空 | 进程内生命周期状态 | 重启后重新装配；旧代次拒绝新接入 |
| **Execution Kernel** | 身份、授权、命令、Unit of Work、对象版本、幂等、审计、outbox、外部 attempt 和回执 | PostgreSQL 业务事实与执行记录 | 通过原命令/attempt/outbox 身份查询、对账和重试 |
| **Workflow Runtime** | P3 的 run、step、attempt、分支、人工等待、定时、恢复和迁移 | PostgreSQL 持久流程实例 | 从 checkpoint/step 状态恢复，固定原定义和能力版本 |

Component Runtime 的 disposer 不会回滚已经提交的业务事实。Execution Kernel 的提交也不会自动证明外部系统已收到消息。Workflow Runtime 不长期持有数据库事务或 provider 租约。

## 4. 当前基线与 P1 目标

当前 P1 不是完整组织操作系统，而是可靠能力执行的基座。已实现范围包括 Runtime Core、PostgreSQL/Redis provider、身份认证首条切片、内部命令底座、幂等/审计/outbox 和可受控投递组件；完整角色版本、资源授权、ServiceGrant、Agent 委托、WASM、签名准入、业务写 API 和持久流程仍按阶段实现。

当前应用模型为 **19 张表**，主要分为：

| 分组 | 数量 | 当前含义 |
|---|---:|---|
| 身份与组织基础 | 11 | Principal、认证来源、组织根、成员经历、workload、bootstrap 和身份操作 |
| 执行与投递 | 5 | guard、CommandExecution、审计、outbox 和投递 attempt |
| 旧授权基础 | 2 | 现有 role assignment、relation tuple，后续接入完整授权模型 |
| 业务示范 | 1 | 最小 work item |

这 19 张表是当前闭环的事实边界，不是完整目标模型的预先实现。P1 不为了“未来可能需要”一次性增加工作流、蓝图、容量、组织单元和所有连接器表；新增表必须对应已批准的能力和可验证的不变量。

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

## 5. P2 的能力组合模型

P2 才把 P1 的固定演示能力推广为有限、可审阅的组合模型。最小对象如下：

| 对象 | 责任 | 不承担的责任 |
|---|---|---|
| `CapabilitySpec` | 稳定能力 ID、接口/输入输出 schema、所需 action、effect 类型、预算和实现引用 | 不授予调用者权限；不接受任意 Python 代码 |
| `Release`（由 `ReleaseManifest` 表达） | 精确能力/模块摘要、依赖版本、环境、前置 revision、批准与签名 | 不代表已经激活，也不绕过当前授权 |
| `CompositionPlan` | 将能力、配置和依赖解析成确定计划，记录版本、摘要和资源上限 | 不直接修改运行时，也不解释自然语言或任意 DSL |
| `Activation` | 在目标环境按 expected revision 激活计划，记录 observed revision、结果和撤销状态 | 不回滚已提交业务事实，不把半成功报告为整体成功 |
| `ExecutionSnapshot`（P1 可由 `FactSnapshot` 与认证证据组合承载） | 一次执行使用的能力绑定、身份证据、授权/撤销 epoch、事实版本和输入摘要 | 不是长期允许令牌，不替代提交前重验 |

`CapabilityBinding` 连接应用注册和 Component Runtime 的 generation/registration；它只表示当前 handler/provider 可以被租用。能力组合和运行时装配是两个动作：前者生成确定计划，后者在受信任 composition root 中按计划装配组件。

组合计划可以引用固定的 WASM manifest、可信 Python handler 或受控连接器 adapter，但每种 effect 都必须声明外部副作用、幂等和恢复语义。计划不能改变 Stage 的信任根、认证规则、授权判断或数据库锁协议。

## 6. P3–P5 的演进

| 阶段 | 运行时扩展 | 进入条件与验收 |
|---|---|---|
| **P3** | 持久 Workflow Runtime：run/step/attempt、分支/汇合、循环、人工等待、定时、恢复和迁移 | P2 的版本、授权和发布契约稳定；验证重启恢复、重复恢复、撤销竞争、外部 unknown、旧定义保留和人工等待不占事务/租约 |
| **P4** | 多进程、多节点、持久协调、fencing、版本化 worker、desired/observed revision 和滚动升级 | 有容量或故障证据；验证节点失联、旧 worker 回写、重复投递、混合版本、租约过期和备份恢复 |
| **P5** | 受控 WASM hostcall、WIT/Component Model、扩展 SDK、有限 HMR 或隐私互操作 | 存在真实消费者、维护资源和威胁模型；每种宿主能力、扩展权限和协议独立验收 |

P4 以前不把单进程 Core 宣称为分布式协调器。P5 以前不把纯计算 WASM 宣称为安全沙箱或隐私证明。

## 7. 四组必须保持的区别

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

## 9. 短期禁止事项

P1 与 P2 早期明确禁止：

- 把 Cordis、Dify、Graphon 或其运行时作为 Stage 生产依赖；
- 直接采用 Dify DSL、Graphon 调度器或兼容层作为 Stage 流程协议；
- 实现任意 Python 插件上传、在线源码执行、通用 HMR 或插件市场；
- 把 ComponentContext 扩展成 Agent/request/identity/workflow 的万能上下文；
- 用 Redis lock、TTL 或内存队列代替 PostgreSQL 权威租约、outbox 和 fencing；
- 在没有真实消费者、容量和故障证据前拆多节点、微服务或通用 workflow engine；
- 因为目标架构包含对象就提前创建完整工作流、容量、组织和跨企业 schema。

相关文档应引用本文作为运行时演进决策入口；历史 review 继续保留其原始范围，不因本决策重写。
