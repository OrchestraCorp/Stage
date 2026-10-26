---
title: Stage 后续任务业务切片
status: design-proposal
implementation_status: not-started
policy_effect: none
revised_date: 2026-10-25
---

# Stage 后续任务业务切片

2026-10-24 按“基座优先”的一期方向，将原一期方案中的完整任务业务设计保留于此。它是 P1 基座完成后进入 P2 的业务切片，不能计入当前 P1 已实现范围。P1 先验证自研 Core、真实认证协议、当前授权、受控 WASM、签名准入及最小资格评估闭环，见[一期方案](phase-1.md)。

下列任务、容量、交付、独立验收规则仍为设计草案，不是企业已生效政策。实际身份适配和共同对象以[身份规范](identity-and-authorization.md)、[核心对象](core-object-contracts.md)为准；本文件只补充任务领域规则。安全与事务基础由一期提供，扩展任务业务时需重新验证新增并发路径。

早期协作边界是[人员—公司—人员](people-company-collaboration.md)：公司承载共同规则、成员任职和协作记录，人员承担发布、承诺、交付及验收责任。Agent 是受人员监督的工具，不是独立成员、任职者或任务承接人；本切片不建设 Agent 登记、承载绑定、Delegation 或独立 Agent 业务接口。工具可辅助生成建议、材料与操作请求，Stage 仍验证实际人员身份及其所选任职；需要本人确认的动作必须有真实确认依据。

本文的 `tenant_ref` 和 guard 沿用内部执行层的兼容名称，均指单个部署实例的组织授权根。业务样例中的多个组织是根下的业务组织单元/范围，不是第二授权根；组织单元和 scope 不自动等同。跨独立 Stage 的主体与 token 分别管理，见[组织授权根语义](organization-root-semantics.md)。

存储选型已于 2026-10-25 确定为唯一主库 PostgreSQL 17＋辅助 Redis 8，见[存储策略](storage-strategy.md)。任务、承诺、容量、幂等结果、当前授权、审计、outbox 和权威业务租约留在 PostgreSQL；Redis 缓存、短期提示或唤醒清空后不丢事实，也不提供替代撤权检查的允许结果。

## 1. 任务与容量的默认语义

| 命令 | 前置条件 | 原子结果 |
|---|---|---|
| `CreateTask` / `task.create` | 创建范围内有能力；稳定创建引用唯一；`expected_version=0` | 创建 draft 工作项与 owner / reviewer 约束，版本为 1 |
| `PublishTask` / `task.publish` | draft；验收标准、责任与资格规则齐全；版本匹配 | 转 open，记录发布规则快照及事件，版本增加 |
| `ClaimWork` / `task.commit.create` | open；单名额空闲；人员的当前成员资格、选定任职与领取资格有效；容量足够 | 创建唯一有效承诺，占用一个名额和一个容量单位，转 committed，版本增加 |
| `SubmitDelivery` / `task.delivery.submit` | 本承诺责任主体；committed / executing；版本匹配 | 新增不可变交付修订，转 awaiting_acceptance，版本增加，容量仍占用 |
| `ReviewDelivery` / `task.acceptance.review`：accept | 当前待验收交付；独立 reviewer；工作项与交付版本匹配 | 新增验收记录，交付 accepted、承诺 fulfilled、任务 closed；释放名额占用与容量一次 |
| 同一命令：reject | 同样要求独立 reviewer；必须填写返工原因 | 新增拒绝记录，交付 rejected、任务 executing；承诺继续有效，容量保留，后续可提交新修订 |

领取成功即正式承诺；提交交付不等于验收，驳回不会偷偷释放容量或重新生成一份承诺。
closed 任务不重新开放，不因名额占用记录释放而允许再次领取；取消、转派、多名额、动态容量计价后续另定。
业务切片最终验收只接受有效的人类 reviewer；Agent 可整理建议，不代签最终接受或驳回。
accept 和 reject 均禁止自审：reviewer 的人员 Principal 不得与交付提交人或承诺责任人重合。比较的是稳定人员身份，不是任职、客户端或使用的工具。
记录经过认证的人员、所选任职、原始业务命令与技术执行来源；同一个人切换任职、账号入口或辅助工具仍不能成为自己的独立 reviewer。工具生成的建议不构成人已批准的证据。
创建者可以是 reviewer，但若后来承接或提交了该任务，也必须由独立 reviewer 验收；平台运维权限不提供例外。
新增 `task.create`、`task.publish` 能力和相应策略；业务 handler 不写角色名称分支。生产授权通过显式发布角色新 revision 与对应任职授予，新 action 不自动加入已有任职；旧可信 fixture 的 RoleCode 映射不能作为数据库授权真源。

创建时的 `expected_version=0` 仅是 CreateTask 命令层“目标尚不存在”的约定，不能传入 AuthorizationContext 的当前 / 预期对象版本。创建授权针对已存在且受权的组织、部门或项目父范围，不要求新任务已有 owner_of、状态或版本；事务内以租户内稳定 creation_ref 的唯一约束保证未重复创建，插入 version=1，并同事务建立 owner / reviewer 关系。现有授权内核对已存在对象的正整数版本要求保持不变；发布、领取、提交与评审仍使用正整数版本。

当前 CommandEnvelope 本身只接受已有对象的正版本，也没有创建 handler；P2 实现创建时须明确新增创建合同与父范围授权协议，不能只向现有命令传 0，或削弱已有对象版本校验来适配创建。

创建请求中的 owner / reviewer 引用只是选择项；服务端必须核验它们属于同一租户且满足有效成员、任职和责任资格。新任务分类受父范围规则与创建者分类上限约束，不能因目标尚不存在而跳过这些输入约束。

业务切片演示数据全部标记 fictional，不写入真实企业目录：

| 样例 | 相同组件下的差异 | 必须得到的结果 |
|---|---|---|
| 虚构组织 A | 工程范围内公开领取；发布者、承接人、独立 reviewer 分离 | 合格成员领取并交付，独立 reviewer 接受后释放容量 |
| 虚构组织 B | 同一 work 模块；领取资格限定到指定部门，承接人使用受监督工具辅助交付 | 跨部门成员被拒绝；交付归属于经过认证的承接人，独立 reviewer 驳回后返工，不重复预留容量 |

这两种配置使用有限类型的资格条件和固定能力点，不执行自然语言政策、动态 Python 表达式或任意插件。
fixture 的容量额度、人员名和组织名只用于复现，不构成通用企业默认规则。
附件本业务切片只接收已受权的稳定 artifact 引用与不可变摘要；不自动抓取调用者任意 URL，不把任务读取权扩张成附件原文读取权。

## 2. 命令注册、结果与幂等

受信任 work 组件通过 `CommandSpec` 声明命令 ID / 版本、输入输出 schema、所需能力与处理器。当前宿主显式传入固定命令集合，外围组件调用租约保护操作，足以承载首条 P2 业务闭环；不要求先实现通用注册平台。

如果实际组合场景需要动态命令贡献，再实现归属当前组件的注册句柄/disposer、重复 ID 拒绝与旧句柄不可删新 handler，并单独验收。同一命令开始后固定本次 handler / provider 代次；依赖消失先停止接收新命令，再排空在途命令，最后释放资源。FastAPI 路由在启动时固定，本业务切片不热改 router 或 OpenAPI 文档。

命令结果保留 `request_id`、`command_execution_id`、`decision_id`、对象版本和可读原因码，至少区分：

| 结果 | 含义与处理 |
|---|---|
| `succeeded` | 业务事务已确认提交；通知尚未投递不改变命令成功 |
| `denied` / `expired` | 授权明确阻断；没有业务状态副作用 |
| `needs_approval` | 需要尚不存在的批准；业务切片不自动创建通用审批流程或当成功处理 |
| `conflict` | 版本、幂等参数、单名额或容量冲突；返回稳定原因码 |
| `unavailable` | 入口或依赖不可用；仅明确受理前拒绝才能断言未执行，普通超时/503 仍按原 key 查询后恢复 |
| `pending` | 已有可追踪执行 / outbox 工作待处理；返回状态查询引用，不假称成功或失败 |
| `unknown` | 数据库提交回执或外部回执无法确认；先按稳定 ID 对账，不换 key 盲重放 |

幂等唯一范围固定为 `(tenant_ref, authenticated_actor_ref, command_id, idempotency_key)`；`command_id` 包含协议版本。
记录另存 `request_digest`；digest 不进入唯一键，否则同 key 不同参数会被误认为两个合法请求。
digest 基于版本化的规范化输入，包含目标、业务参数、预期版本及所选择任职引用；排除 token、trace ID 和服务端评估时间。
同唯一范围、同 digest 返回同一业务执行结果；不同 digest 返回冲突，事务不得写入第二份业务事实。
认证 / 输入校验失败属于未受理，不占幂等 key；已受理的明确拒绝或业务冲突可提交 execution 终态与审计，不写领域状态。
未提交的 execution 随事务回滚；相同 key 后续可重新受理。已提交终态保持稳定，调用者需要改变业务意图时必须使用新 key。
重放结果前仍验证当前认证、租户和结果读权限；授权已撤销时不能借旧 key 再写或读取受限结果。
重放读取已有结果不重新执行 handler；执行状态和响应摘要必须与业务变更在同一事务内持久化。
异常发生在确认提交之前时，由服务端以原唯一范围查询执行记录；客户端通过原 key 或 execution ID 查询受权状态。

## 3. 事务、并发与撤销协议

业务切片以清楚、可测的协议换取较小的并发面：唯一组织根建立持久 `AuthorizationGuard` 行，全部受控业务写和授权变更先锁此行。
任职、关系、资格、主体状态、分类上限及有效策略的授予 / 撤销都必须走受控命令并锁同一 guard。
不允许旁路 SQL / 插件直接修改这些权威事实；否则下面的串行化保证不成立。
当前私有化部署仅有一个组织授权根。主体禁用或共享策略变更遵循 TrustState → Principal → 部署组织 guard 的共同锁序，锁后重验全部受影响主体/任职；部门和项目使用 scope。根不等同法人公司，组织单元与 scope 的边界见[组织授权根语义](organization-root-semantics.md)。详情见[一期事务协议](phase-1.md#transaction-protocol)。
业务切片不能完整确定受影响主体、信任配置和授权事实依赖的共享变更应被拒绝；不能只锁组织 guard 却跳过前序主体/信任锁而宣称完成全局撤销。

遵循[一期全局锁序](phase-1.md#transaction-protocol)，跨对象集合按稳定ID排序；以下仅列任务域的具体锁对象，省略不涉及的批准/激活行：

```text
BEGIN
→ 相关 TrustState → 全局主体状态（按需）
→ tenant AuthorizationGuard FOR UPDATE
→ 工作项 → 承诺 → 交付
→ 按责任主体 ID 排序的容量账户与 reservation
→ 幂等唯一范围 / execution 记录
→ 读取当前人员身份、成员、所选任职、关系、分类、策略与资格
→ authz 判定 + 版本比较 + 独立验收 / 容量 / 状态不变式
→ 写状态、版本、执行结果、审计、领域事件和 outbox
COMMIT
```

这一共同锁点在 READ COMMITTED 下将同租户的授权变更与业务写按实际锁顺序排列，不声称数据库隔离级别变成 SERIALIZABLE。
撤销先提交，则随后命令必须读取撤销结果并拒绝；命令先持锁并提交，则其业务事实发生在撤销之前，撤销不倒改历史。
业务切片接受同租户业务写被 guard 串行化的吞吐代价；以后拆细锁域前必须提供等价不变式与真库并发证明。
所有查询同时按 tenant 过滤，任务与相关对象的租户不匹配即拒绝；不能依赖全局 ID 碰巧不重复。
授权的时间判定采用事务内最终重验时的统一 UTC 时钟；该点是本次业务写的授权线性化点，长事务必须有时间上限。
获得对象 / 容量锁后重新取当前时间，重验到期、撤销与版本；入队时或事务外的旧允许结果不够。
最终重验同时检查入口凭据到期时间与命令 deadline；持锁期间已经过期的认证结果不得继续写入。
领取对 slot 与容量进行锁内检查 / 条件更新，并用唯一约束兜底；失败回滚承诺、占用、版本和 outbox，不能留下半份结果。
接受交付以 reservation 状态与唯一引用控制释放，更新容量不得小于零；重复接受或重放不二次释放。
发生死锁或可重试事务错误时，有限次数重启整个事务、重载事实并重验；不从中间步骤继续，也不更换幂等 key。
提交连接丢失归类 `unknown`，恢复后查原 execution；不能把数据库超时直接当作确定回滚。
对账使用独立会话，在取得同一 guard、确认原事务已结束后读取执行记录；原锁仍未释放或状态无法确认时继续保持 unknown。
仅一次查询未见记录不能证明没有执行；确认原事务回滚 / 未提交后，才允许用原 key 重走完整命令路径。

## 4. 最小持久模型草案

下表是业务切片待加字段 / 表，不是现有 schema 的描述。租户内业务表具有稳定引用、`tenant_ref`、UTC 时间与必要索引；外键或等价检查禁止跨租户关联。
部署级主体与组织根成员经历分表，主体引用带来源命名空间；主体本身不授予根下任一组织单元或资源的业务读取权。

| 对象 | 业务切片最小字段与约束 |
|---|---|
| `Principal` / `OrganizationMembership` | 人员 principal_ref、principal_type=human、issuer_subject_ref、status；同组织根/principal 最多一条未终结经历、历史成员 ref 不复用、成员状态 / version；物理成员表沿用 tenant_membership |
| `AuthorizationGuard` | tenant_ref 唯一、epoch；每次有效授权变更在锁内增加 epoch，记录审计 |
| `RoleAssignment` / `RelationTuple` | 复用 P1b.2 已实现的人员成员经历、固定角色版本、scope 与直接 ACL 基础；任务关系补充领域权威来源、有效期和撤销状态，不另造 Agent 任职路径 |
| `WorkItem` | 现有标题 / 状态 / 分类 / version，新增 tenant、稳定 ref、owner_ref、reviewer_ref、验收标准、资格规则版本、capacity_cost=1、slot_limit=1、active_slot_ref |
| `Commitment` | work_ref、responsible_principal_ref（人员）、membership_ref、assignment_ref、actor_ref、status、version；一个任务至多一个有效承诺 |
| `CapacityAccount` / `CapacityReservation` | tenant + responsible_principal 唯一；total / reserved / version；reservation 绑定 commitment、units=1、active / released、released_at，承诺引用唯一 |
| `Delivery` | work_ref、commitment_ref、revision、artifact_ref / digest、submitter_principal_ref（人员）、assignment_ref、status、version；work + revision 唯一，内容修订不可原地覆盖；工具辅助来源可作非授权元数据 |
| `Acceptance` | delivery_ref / revision、reviewer_principal_ref（人员）、assignment_ref、accept / reject、reason、decision_id、reviewed_at；同一交付修订仅一个最终评审结果；需要确认时保存人员确认依据 |
| `CommandExecution` | 幂等唯一四元组、request_digest / digest_version、目标、状态、结果摘要、request_id、decision_id、版本、完成时间；敏感结果引用受权读取 |
| `AuditEntry` / `DomainEvent` | 稳定 ID、经验证的人员 principal / membership / assignment、action、target、before / after 或受限引用、对象版本、request / execution / decision / causation ID；另记技术执行来源，不能代替人员批准 |
| `OutboxMessage` | message_id 唯一、event / aggregate 引用、payload 版本、pending / processing / succeeded / failed / unknown、attempt、next_attempt_at、lease_until、last_error_code、dedupe_key |

单有效承诺、active slot 和容量不变式分别由 PostgreSQL 事务锁与相应数据库约束实现；新增约束写入迁移并通过 PostgreSQL 17 真库门禁，不再为 MySQL 保持同构实现。SQLite 快测的适用范围单独说明。
受限正文与凭据不得写进普通审计 / 日志；审计、outbox 和命令结果继承来源读者边界。

## 5. API 草案与持久 outbox

以下是待评审接口，尚未写入 `contracts/openapi.yaml`；实现时必须同步模型、稳定错误码、OpenAPI 导出和契约测试。

| HTTP 草案 | 命令 / 查询 |
|---|---|
| `POST /api/v1/tasks` | CreateTask；稳定 creation_ref、expected_version=0 |
| `POST /api/v1/tasks/{ref}/publish` | PublishTask；expected_version |
| `POST /api/v1/tasks/{ref}/claim` | ClaimWork；expected_version、选定 assignment 引用 |
| `POST /api/v1/tasks/{ref}/deliveries` | SubmitDelivery；commitment_ref、expected_version、artifact 引用与摘要 |
| `POST /api/v1/deliveries/{ref}/reviews` | ReviewDelivery；accept / reject、reason、任务及交付预期版本 |
| `GET /api/v1/tasks/{ref}` | 受权工作项、承诺、当前交付与验收投影 |
| `GET /api/v1/command-executions/{ref}` | 受权结果查询；支持按原幂等范围恢复查询 |

所有写请求要求 `Idempotency-Key`；主体来自认证，body 不接受可信 actor / tenant / classification ceiling。
成功 200 / 201、已受理 pending / unknown 的可查询状态 202、身份失败 401、授权失败 403、版本 / 幂等冲突 409、输入错误 422、依赖不可用 503；404 / 403 的资源隐藏规则统一评审。
`pending` / `unknown` 的原因保留在结果类型中；只有已持久受理或已有稳定执行 ID 可追踪时才返回 202，未受理请求不可伪造查询承诺。

outbox 必须与业务变更同一数据库事务写入，提交后 worker 才投递；通知失败不把已经确认的领取改成失败。
Redis 可以在提交后发出可丢失唤醒，不能作为 outbox 的唯一保存或领取来源；遗漏唤醒由 PostgreSQL 扫描恢复，不承诺两种存储双写原子性。相关缓存按 tenant 与当前权限版本隔离，Redis 故障不能放宽领取、验收或结果读取授权。
同进程 worker 也使用持久 lease / claim 记录，进程重启后恢复未完成消息；不能依赖内存队列作为唯一来源。
投递为至少一次：稳定 message_id / dedupe_key、接收端去重或幂等；不承诺 exactly-once。
业务切片通知 sink 是虚构、可控的测试接收端，覆盖成功、拒绝、超时和“已执行但回执丢失”。
超时未知时先查稳定外部引用 / 回执再决定重试；无安全幂等或无法对账的操作进入 unknown 待处理，不无限重放。
worker 只领取已由受权命令持久创建的技术工作；记录原始人员、成员经历、任职、命令和因果引用，技术身份本身不成为任务承接人或验收人。继续执行人员发起的受权业务或受限外发前，按该动作协议重新加载当前人员权限、接收范围和对象状态；撤销后的待执行消息按策略阻断并保留原因。已发操作的回执核验与 unknown 对账保留为受控技术恢复，不能借恢复再发出无权业务动作，也不能伪造人员当前登录或确认。
worker 先在短事务中锁 guard 与消息，记录最终投递授权及 attempt，再释放事务锁并调用测试 sink；不持有数据库事务等待外部网络。
这里的授权线性化点是受控投递被登记开始时：此前提交的撤销阻止投递，此后到达的撤销记录取消请求并处理在途结果。
已发出的外部操作不能与本地撤销组成原子事务；即使在途取消，也必须保存实际成功 / 失败 / unknown，不宣称已撤回。
消息的 dedupe_key 与业务消息稳定绑定，attempt 增加不生成新的业务幂等身份；消息重试采用有上限的退避并保留人工处理入口。
高影响外部动作本业务切片不启用；未来接入时单独验收接收端授权、幂等、取消、回读与补偿协议。
runtime cleanup 负责停止订阅 / 定时器、排空 worker、释放连接；已发送通知用补偿动作更正，不能借卸载声称撤回。

## 6. 九类验收与证据

1. **Core 生命周期**：声明依赖等待 / 恢复、接口不兼容、重复注册、失败清理、停机排空、在途命令不被卸载中断；全套对应 Runtime 规范。
2. **可信身份与上下文隔离**：fixture 只在 dev / test，prod 启动拒绝；HTTP / CLI / worker 同路径；并行主体/任职请求无共享主体、任职或 Session 泄漏；IdP 契约测试与真实部署集成分开记录。
3. **权限与数据边界**：伪造组织根、跨 scope 或部门未授权访问拒绝，scope 不因组织根成员身份扩大；未知分类、失效关系、未知策略、缺能力拒绝；查询、结果、审计和通知投影无侧漏。
4. **授权撤销与时间竞争**：用两个真实数据库连接和屏障控制“命令先持 guard”与“撤销先提交”两种顺序，覆盖成员、任职、关系、主体状态；锁后到期、排队后撤销均重验；证明共同锁点和超时策略成立。
5. **幂等与版本**：同 key 同 digest 一份事实；同 key 不同参数冲突；不同 actor / tenant 的 key 不串结果；旧版本、非法版本、事务回滚、响应丢失后查询、当前权限撤销后的重放均覆盖。
6. **领取与容量并发**：不同申请者同时争一个名额仅一个成功；同主体争两个任务不能超容量；无半份承诺、负容量、重复占用；锁序 / 死锁重试经过 PostgreSQL 17 真库测试。
7. **完整业务与独立验收**：create → publish → claim → submit → accept 关闭并释放一次；reject 保留承诺 / 容量，返工重交后由独立人员接受；提交者换任职或使用辅助工具仍不能自审，accept / reject 同样阻断；自动生成内容不能被记作人已确认。
8. **outbox 与故障恢复**：事务失败零消息；事务成功消息可恢复；重复投递接收端去重；提交断连、worker 崩溃、lease 过期、外部已执行回执丢失分别验证 pending / unknown / 对账路径，不盲重放。
9. **迁移、契约与可运营性**：PostgreSQL 17 从空库升级及前向升级、约束和 UTC / 标识符语义正确；Redis 使用方按故障策略验证超时、不可用、清空及失效/唤醒遗漏，不丢事实、不越权；OpenAPI 无漂移；请求 / 命令 / 决策 / 事件可追踪；脱敏、优雅关闭、备份恢复演练与运行局限有记录。

每项记录环境、数据库版本、精确执行命令、结果、失败与剩余风险；通过 SQLite 或编译 DDL 不能替代真库证据。
独立的授权、关系、属性、版本、事务与 runtime 边界测试必须保留；一次端到端演示不能代替上述门禁。
缺少生产 IdP adapter、密钥 / 凭据管理、部署访问边界、迁移与恢复流程时，业务切片产物只能作为受控开发验收环境交付。
