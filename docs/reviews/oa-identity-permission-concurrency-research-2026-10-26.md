---
title: 成熟 OA 与企业应用的身份、权限及并发实现调研
status: research-only-not-an-approved-implementation-spec
date: 2026-10-26
policy_effect: none
---

# 成熟 OA 与企业应用的身份、权限及并发实现调研

本轮承接[组织锁成熟方案调研](organization-lock-industry-research-2026-10-26.md)，补看企业应用实际实现。只读取官方源码、固定版本文档及公开产品说明；未运行这些产品、未进行安全认证，也未修改 Stage 应用、测试、数据库或依赖。Stage 对照提交为 `0560abef3872ad40d406ad4e8a54b3ad35f800e5`。

## 1. 样本范围与主要结论

| 样本 | 类型与固定阅读版本 | 研究用途 |
|---|---|---|
| O2OA | 实际 OA／协同办公平台；`develop-10.1-ce`，`f6a85b5d995d570433cdbec5aa2a791341e7bfb7` | 组织身份、待办权限、流程实例执行、组织缓存 |
| Frappe／ERPNext／Frappe HR | 企业应用框架、ERP 与人事审批；Frappe `v15.50.0` / `e6e3cc1413827247c0887808c6e155bd7f8351e6`；ERPNext `v15.50.0` / `c5cd0fcd29138ca5736fdde938a36e3528c9fda5`；HRMS `v15.40.0` / `88791bbf5b6b26c7077119021e687554272933fa` | 角色／数据范围／单据分享、状态迁移、请假与采购审批、单据并发 |
| Flowable Open Source | BPMN 流程引擎，**不是完整 OA**；`flowable-7.2.0` / `11a1cf83a8c03e343040b4d8edc751f9534d7323` | 任务参与关系、实体乐观锁、实例级作业协调、事务边界 |
| RuoYi-Vue | 国内常见管理脚手架，**不是完整 OA**；`v3.9.2` / `0e2d75c23c0d7a1fa85f660f06a59a4dd1ba14c0` | 功能权限与部门数据范围分离、登录会话权限缓存 |

这些是可复核的版本样本，未宣称其为当日最新版本；Frappe、ERPNext 与 HRMS 分别阅读，不宣称上述组合已经部署兼容验证。泛微、致远、钉钉等闭源产品未获得内部实现证据，本轮不根据产品宣传推断它们的锁或撤权协议。

**共同的可借鉴方向是：权限分层、业务实例内协调、一次操作内事实复用、每次推进采用短事务。** 样本中没有证据说明“全部普通业务必须按企业根排成一条队列”；同样不能从几个入口未见组织锁，就断言产品全仓没有广域锁。

最需要保留的区别是：这些系统的缓存失效、当前待办检查、对象版本冲突，不自动等价于 Stage 当前的“撤权与业务提交明确排序”。这一保证是否保留、哪些操作适用，需要明确设计，不能以参考成熟产品为由悄悄放宽。

## 2. O2OA：人员、组织身份与流程待办分别建模

### 2.1 人员不等于组织身份，也不等于某个待办的处理资格

O2OA 的 `Identity.person` 关联人员，`Identity.unit` 关联组织；同一人员可以有多个身份，`major` 记录主身份。创建身份时会读取同人的其他身份并维护主身份标志，没有把“一个人只能有一条组织身份”作为约束。[O1]

`Unit.superior` 表达组织层级；`UnitDuty` 保存所属组织与身份成员集合。流程 `Task` 另持久保存 `job/person/identity/unit`，把人员、办理组织身份和流程实例关联起来。[O1][O2]

**Stage 借鉴点：** Principal、成员经历、任职、任务责任分别表达是合理的。内部部门、职务、项目组织与“一实例一个组织授权根”不冲突；但 O2OA 的组织身份模型不是 Stage 自动增加多企业／多租户的理由。

**重要差异：** O2OA 的 AuthInfo 会聚合一个人的身份、组织、职务、群组和角色；Stage 当前业务明确选择一个任职，不自动并集全部身份权限。这一语义不能照搬。[O5]

### 2.2 操作权限拆分，并复用本次已计算事实

`WorkControlBuilder` 区分读取、保存、处理、管理等控制。任务处理入口读取当前 Task 和 Work，再检查 `allowManage || allowProcessing`；处理资格中的一个实际判断是查询当前 `person + work` 是否存在待办，管理资格走另外的规则。[O3]

builder 内的 `canManage`、`hasTaskWithWork` 等结果使用实例字段复用，避免同一次控制计算多次查询。它不是无条件跨请求缓存整个“允许”结果。

**Stage 借鉴点：** 同一受保护操作中，把已取得并受保护的事实做成明确、有限的读取结果；多个能力判定复用该结果。不要为了封装干净，让每个 helper 都重新查完整身份链。

### 2.3 必要串行落到 job，并在实际执行时重读任务

处理服务取得 `Task.job` 后调用 `ProcessPlatformKeyClassifyExecutorFactory.get(job).submit(...)`。factory 为每个 key 创建一个单线程池；任务真正开始执行时再次读取 Task，然后在事务内将 Task 转成 TaskCompleted。[O4]

这提供一个具体的工程样本：**同一个流程实例排队，不同实例可以并行。** 它同时保留局部串行与执行时当前状态检查，没有把所有冲突都交给一个企业级互斥点。

surface 服务还以 job 为 seed 选择处理节点，节点选择函数使用 seed 的 CRC32 对节点列表取模。[O4] 这只证明节点列表稳定时的定向路由＋节点内执行器串行。本轮没有验证拓扑变化、故障接管、旧节点 fencing，不能把内存队列当成完整的分布式排他保证。

**Stage 不宜直接复制实现：** Python 进程内“每对象一个队列”不能代替 PostgreSQL 的持久并发协议；多个 worker、重启、超时和结果未知都仍需处理。可借鉴的是冲突键限定在对象／实例，现阶段优先利用已有数据库事务和版本协议。

### 2.4 缓存失效与提交屏障分开看

组织 AuthInfo 有缓存；身份编辑提交后通知 Identity／Person 相关缓存失效，缓存设施支持 Redis 或 Guava。[O5] 这是组织数据复用和失效策略的证据，不足以证明所有在途业务会与撤权事务按 Stage 的合同排序。

## 3. Frappe／ERPNext／Frappe HR：单据权限与状态迁移

### 3.1 权限分为多个实际层次

Frappe `has_permission()` 结合 DocType 权限、角色、owner、User Permission 与文档分享；User Permission 可以限制某类关联值及其子级范围。角色权限计算会把用户多个适用角色的允许项合并，并有请求内结果缓存；角色列表及用户数据范围还有服务端缓存。[F1]

`User.on_update()` 清理用户缓存；User Permission 的更新／删除清理对应缓存，并在提交后发送更新通知。[F2] 这说明“使用缓存”通常伴随明确失效入口；本轮没有证明这些入口具备严格的在途撤权提交屏障。

**Stage 借鉴点：** 菜单或功能是否可见、哪些数据能列出、某张单据能否操作，应该分别定义。列表查询可在数据库中下推范围条件，但详情和写命令仍使用相同业务权限语义。角色自动并集与 Stage 当前单任职规则有差异，不直接移植。

### 3.2 审批是状态迁移，不只是“有审批角色”

`get_transitions()` 检查当前单据状态、用户角色和 transition 条件。`apply_workflow()` 从数据库重新加载单据、重新找到允许的 action，并检查自我批准规则，然后进入 save／submit／cancel。[F3]

ERPNext 采购单提交路径还调用 `Authorization Control.validate_approving_authority(doctype, company, base_grand_total)`，对应金额、角色或审批人员规则。[F4] 因而金额等 ABAC 条件不是只能放在通用 IAM 中，也会出现在具体领域提交约束中。

**Stage 借鉴点：** 未来批准应绑定明确动作、单据版本、当前状态、人员责任与业务条件。登录 MFA 只能证明近期认证，不能自动证明对具体金额、版本或意图的批准。

### 3.3 请假审批提供了关系更新的具体例子

Frappe HR 的默认请假审批人先取 Employee 的指定审批人，否则取 Department Approver；请假单保留审批人字段。[F5]

`share_doc_with_approver()` 在审批人没有 submit 权限时建立对应单据分享；审批人变更时移除旧审批人的该项分享。请假提交还校验员工有效性、状态、业务约束；未配置 Workflow 的路径有单独的“禁止本人批准”设置，Workflow 路径则使用自己的 self-approval 控制。[F5]

这说明需要明确区分：**路由上选中的审批人、该人的单据权限、真正发生的审批动作和历史记录。** 自动建立 submit 分享是该产品的实际选择；Stage 是否按任务关系派生受限能力，应独立规定，不能一分配任务就隐式授予任意写入权。

简单的“当前用户不是 owner”也不等于我们未来所有双人规则的完整证明；多人、代理、移交、变更版本后的重批等仍要单独设计。

### 3.4 单据保存采用局部锁＋修改版本比较

Frappe `_save()` 检查写权限，再执行 `check_if_latest()`。后者通过 `load_doc_before_save()` 以 `for_update=True` 读取这张单据，比较 `modified` 与调用者原始版本，不匹配则抛出 TimestampMismatchError。[F6]

这是**单据行锁与乐观版本检测结合**的实际例子，不能简化描述成“完全无锁”或“只比较时间戳”。普通请求的提交由框架事务边界完成；官方文档说明成功写请求通常在请求末尾提交，未捕获异常回滚。[F7]

Stage 已有 expected_version、命令事务、审计和 unknown 恢复，可以借鉴其对象范围；无需为了参考产品改成整套 DocType ORM，也不能从对象并发正确性推导任职撤销同步已经解决。

## 4. Flowable：工作分配关系不自动等于安全授权

### 4.1 引擎 API 的信任边界

Flowable task 有 assignee／owner，identity link 有 candidate 用户或组。默认 `ClaimTaskCmd` 主要防止任务已被别人领取；默认完成路径检查任务存在、活跃及委派状态，但不会自动执行完整的“当前认证用户必须有业务办理权限”检查。[B1]

这是面向可信应用的嵌入式引擎 API，不是最终用户的完整安全门面。应用可以增加授权门面或拦截器；不能据此认定采用 Flowable 的 OA 产品一定存在越权。

`taskCandidateUser` 查询会解析当前用户组；删除 IDM 成员关系的命令自身只删除成员关系，不自动撤回已独立记录的任务 assignee。[B2]

因此，“员工调走后，已经领取的待办怎么办”是必须写清的业务规则：历史责任保留、办理权冻结、移交或重新分派，不能靠候选列表变化隐式决定。Stage 的关系事实应进入当前授权判断，而不直接绕过统一入口。

### 4.2 实体乐观锁与流程实例范围协调

Flowable 更新任务时使用 `WHERE ID_ = ... AND REV_ = ...`，同时推进 REV_；更新匹配零行由执行器报告乐观冲突。[B3]

默认 exclusive 异步作业的协调范围是流程实例：实际路径找到 processInstanceId，通过执行实例行的 `LOCK_TIME_ / LOCK_OWNER_` 条件更新取得租约。[B4] 不同实例可以并行，同实例中容易冲突的推进保持必要顺序。

**Stage 借鉴点：** 把业务冲突定位到工作项／未来流程实例；账号、会话、幂等键各有自己的消费或修改冲突。组织授权保护仍需要表达，但不应兼做所有业务对象的总队列。

### 4.3 长流程由多个短事务推进

引擎从一次触发推进到下一等待点并持久化，人员等待几小时不占着数据库事务。异步 continuation 在事务内创建 job，提交后在新事务中继续。[B5]

Flowable 的 Spring 集成可加入调用方事务，使共享事务资源下的业务修改与流程推进一起提交。如果 Stage 通过 HTTP 调用独立 Java 流程服务，两边不会自动成为一个原子事务。不能因引入成熟引擎就省略 outbox、重试、对账与未知结果。

当前 Stage 尚无需要引入完整 BPMN 引擎的已验收业务切片；这里借鉴事务粒度和冲突范围，不据此扩大近期路线。

## 5. RuoYi-Vue：功能权限和数据范围分离，缓存语义另审

`DataScopeAspect` 支持全部、自定义部门、本部门、本部门及子部门、仅本人等范围，按角色、部门和权限字符生成 SQL 条件。它与功能权限字符串分别处理。[R1]

功能检查读取 LoginUser.permissions，LoginUser 从 Redis 会话取回；getInfo、角色修改、强制下线有各自的刷新或删除路径。[R2] 部分数据范围依赖会话内角色信息，另一些关系在查询 SQL 中读取，不能笼统称所有权限事实每次都实时读库。

**Stage 借鉴点：** 数据范围下推和稳定 action 点。**不直接借鉴：** 自动合并全部角色范围、将登录时的权限集合长期作为关键业务提交权威，或把删除会话缓存当成完整的跨事务撤权协议。

## 6. 对 Stage 的设计启示

| 关注点 | 样本支持的方向 | Stage 应保留／进一步定义的部分 |
|---|---|---|
| 组织与人员 | 人员、组织身份、部门职务分别表达 | 一个组织授权根；当前明确单任职，不自动并权 |
| 业务权限 | 功能动作、数据范围、单据关系、状态条件分层 | 仍由统一授权入口组合，不能只在菜单或待办列表校验 |
| 实例并发 | 单据行锁／版本或流程实例协调 | 幂等键、对象版本、审计、未知结果，与授权同步分别处理 |
| 事实读取 | 一次操作内复用已查事实、明确缓存失效 | 当前锁域／版本证明与最后期限检查；不裸缓存允许结果 |
| 待办与撤权 | 选人、领取、历史责任、办理资格分离 | 岗位撤销／离职后的冻结、移交、历史责任规则需正式定义 |
| 长期流程 | 等待点持久化、每次推进短事务 | 有真实业务后再引入；保留进程重启与外部副作用恢复协议 |

对组织锁优化的结论比前轮更具体：**普通业务以工作项／单据为并发单位；授权变动按其影响范围协调；组织根保护只承担真正的组织级授权一致性职责。** 先在现有最小工作项操作上验证，不提前增加审批表、流程 DSL 或一套新引擎。

我们应当先写出“权限变更与任务处理冲突矩阵”：修改本人资料、读取待办、编辑不同工作项、同项并发修改、撤销任职、变更角色、停用人员、修改 MFA、移交任务，分别改变什么事实、影响哪个范围。再按上一轮 PostgreSQL 研究选择共享行锁或事务 advisory 锁等手段，补足同键竞争、锁顺序和提交前验证。

本轮没有产品实跑或对比性能数据。缓存刷新是否能提供某种严格撤权语义，必须以协议和竞争测试证明；不能因为实现来自成熟 OA 就直接视为满足 Stage 所有安全要求。

## 7. 固定源码证据

完整源码路径、SHA-256、版本及关键行见[来源清单](oa-identity-permission-concurrency-sources-2026-10-26.json)。以下链接均指向固定提交；第三方源码全文未收入 Stage 仓库。

- **[O1]** [O2OA Identity](https://github.com/o2oa/o2oa/blob/f6a85b5d995d570433cdbec5aa2a791341e7bfb7/o2server/x_organization_core_entity/src/main/java/com/x/organization/core/entity/Identity.java#L124)、[多身份创建](https://github.com/o2oa/o2oa/blob/f6a85b5d995d570433cdbec5aa2a791341e7bfb7/o2server/x_organization_assemble_control/src/main/java/com/x/organization/assemble/control/jaxrs/identity/ActionCreate.java#L90)。Unit／UnitDuty 路径见来源清单。
- **[O2]** [O2OA Task](https://github.com/o2oa/o2oa/blob/f6a85b5d995d570433cdbec5aa2a791341e7bfb7/o2server/x_processplatform_core_entity/src/main/java/com/x/processplatform/core/entity/content/Task.java#L148)。
- **[O3]** [WorkControlBuilder](https://github.com/o2oa/o2oa/blob/f6a85b5d995d570433cdbec5aa2a791341e7bfb7/o2server/x_processplatform_assemble_surface/src/main/java/com/x/processplatform/assemble/surface/WorkControlBuilder.java#L213)、[处理入口检查](https://github.com/o2oa/o2oa/blob/f6a85b5d995d570433cdbec5aa2a791341e7bfb7/o2server/x_processplatform_assemble_surface/src/main/java/com/x/processplatform/assemble/surface/jaxrs/task/ActionProcessing.java#L118)。
- **[O4]** [job 级执行入口](https://github.com/o2oa/o2oa/blob/f6a85b5d995d570433cdbec5aa2a791341e7bfb7/o2server/x_processplatform_service_processing/src/main/java/com/x/processplatform/service/processing/jaxrs/task/ActionProcessing.java#L52)、[每 key 单线程执行器](https://github.com/o2oa/o2oa/blob/f6a85b5d995d570433cdbec5aa2a791341e7bfb7/o2server/x_processplatform_service_processing/src/main/java/com/x/processplatform/service/processing/ProcessPlatformKeyClassifyExecutorFactory.java#L49)、[节点选择](https://github.com/o2oa/o2oa/blob/f6a85b5d995d570433cdbec5aa2a791341e7bfb7/o2server/x_base_core_project/src/main/java/com/x/base/core/project/Applications.java#L681)。
- **[O5]** [AuthInfo 聚合与缓存](https://github.com/o2oa/o2oa/blob/f6a85b5d995d570433cdbec5aa2a791341e7bfb7/o2server/x_organization_assemble_express/src/main/java/com/x/organization/assemble/express/jaxrs/person/ActionGetAuthInfo.java#L37)、[身份修改后通知](https://github.com/o2oa/o2oa/blob/f6a85b5d995d570433cdbec5aa2a791341e7bfb7/o2server/x_organization_assemble_control/src/main/java/com/x/organization/assemble/control/jaxrs/identity/ActionEdit.java#L84)。
- **[F1]** [Frappe 权限入口](https://github.com/frappe/frappe/blob/e6e3cc1413827247c0887808c6e155bd7f8351e6/frappe/permissions.py#L77)、[角色聚合](https://github.com/frappe/frappe/blob/e6e3cc1413827247c0887808c6e155bd7f8351e6/frappe/permissions.py#L270)、[用户数据范围](https://github.com/frappe/frappe/blob/e6e3cc1413827247c0887808c6e155bd7f8351e6/frappe/core/doctype/user_permission/user_permission.py#L85)。
- **[F2]** [用户缓存清理](https://github.com/frappe/frappe/blob/e6e3cc1413827247c0887808c6e155bd7f8351e6/frappe/core/doctype/user/user.py#L220)、[数据范围变更通知](https://github.com/frappe/frappe/blob/e6e3cc1413827247c0887808c6e155bd7f8351e6/frappe/core/doctype/user_permission/user_permission.py#L36)。
- **[F3]** [Workflow transition 与 apply](https://github.com/frappe/frappe/blob/e6e3cc1413827247c0887808c6e155bd7f8351e6/frappe/model/workflow.py#L39)、[自我批准检查](https://github.com/frappe/frappe/blob/e6e3cc1413827247c0887808c6e155bd7f8351e6/frappe/model/workflow.py#L221)。
- **[F4]** [采购单提交时检查批准权限](https://github.com/frappe/erpnext/blob/c5cd0fcd29138ca5736fdde938a36e3528c9fda5/erpnext/buying/doctype/purchase_order/purchase_order.py#L476)、[Authorization Control](https://github.com/frappe/erpnext/blob/c5cd0fcd29138ca5736fdde938a36e3528c9fda5/erpnext/setup/doctype/authorization_control/authorization_control.py#L151)。
- **[F5]** [请假审批人选择](https://github.com/frappe/hrms/blob/88791bbf5b6b26c7077119021e687554272933fa/hrms/hr/doctype/leave_application/leave_application.py#L1377)、[单据分享同步审批人](https://github.com/frappe/hrms/blob/88791bbf5b6b26c7077119021e687554272933fa/hrms/hr/utils.py#L675)、[自我批准规则](https://github.com/frappe/hrms/blob/88791bbf5b6b26c7077119021e687554272933fa/hrms/hr/doctype/leave_application/leave_application.py#L799)。
- **[F6]** [Document.check_if_latest](https://github.com/frappe/frappe/blob/e6e3cc1413827247c0887808c6e155bd7f8351e6/frappe/model/document.py#L807)、[for_update 读取](https://github.com/frappe/frappe/blob/e6e3cc1413827247c0887808c6e155bd7f8351e6/frappe/model/document.py#L1115)。
- **[F7]** [请求事务提交](https://github.com/frappe/frappe/blob/e6e3cc1413827247c0887808c6e155bd7f8351e6/frappe/app.py#L404)、[官方事务说明](https://docs.frappe.io/framework/user/en/api/database#database-transaction-model)。
- **[B1]** [Flowable claim 合同](https://github.com/flowable/flowable-engine/blob/11a1cf83a8c03e343040b4d8edc751f9534d7323/modules/flowable-engine/src/main/java/org/flowable/engine/TaskService.java#L169)、[ClaimTaskCmd](https://github.com/flowable/flowable-engine/blob/11a1cf83a8c03e343040b4d8edc751f9534d7323/modules/flowable-engine/src/main/java/org/flowable/engine/impl/cmd/ClaimTaskCmd.java#L41)、[TaskHelper 完成路径](https://github.com/flowable/flowable-engine/blob/11a1cf83a8c03e343040b4d8edc751f9534d7323/modules/flowable-engine/src/main/java/org/flowable/engine/impl/util/TaskHelper.java#L67)。
- **[B2]** [当前候选组查询](https://github.com/flowable/flowable-engine/blob/11a1cf83a8c03e343040b4d8edc751f9534d7323/modules/flowable-task-service/src/main/java/org/flowable/task/service/impl/TaskQueryImpl.java#L1984)、[删除组成员](https://github.com/flowable/flowable-engine/blob/11a1cf83a8c03e343040b4d8edc751f9534d7323/modules/flowable-idm-engine/src/main/java/org/flowable/idm/engine/impl/cmd/DeleteMembershipCmd.java#L36)。
- **[B3]** [Task.xml 版本化更新](https://github.com/flowable/flowable-engine/blob/11a1cf83a8c03e343040b4d8edc751f9534d7323/modules/flowable-task-service/src/main/resources/org/flowable/task/service/db/mapping/entity/Task.xml#L168)、[零行更新冲突](https://github.com/flowable/flowable-engine/blob/11a1cf83a8c03e343040b4d8edc751f9534d7323/modules/flowable-engine-common/src/main/java/org/flowable/common/engine/impl/db/DbSqlSession.java#L611)。
- **[B4]** [按 processInstanceId 协调](https://github.com/flowable/flowable-engine/blob/11a1cf83a8c03e343040b4d8edc751f9534d7323/modules/flowable-engine/src/main/java/org/flowable/engine/impl/cfg/DefaultInternalJobManager.java#L185)、[实例租约 SQL](https://github.com/flowable/flowable-engine/blob/11a1cf83a8c03e343040b4d8edc751f9534d7323/modules/flowable-engine/src/main/resources/org/flowable/db/mapping/entity/Execution.xml#L281)。
- **[B5]** [等待点和事务边界](https://github.com/flowable/flowable-engine/blob/11a1cf83a8c03e343040b4d8edc751f9534d7323/docs/docusaurus/docs/bpmn/ch07b-BPMN-Constructs.md#L4354)、[Spring 业务与流程同事务](https://github.com/flowable/flowable-engine/blob/11a1cf83a8c03e343040b4d8edc751f9534d7323/docs/docusaurus/docs/bpmn/ch05-Spring.md#L95)。
- **[R1]** [RuoYi DataScopeAspect](https://github.com/yangzongzhuan/RuoYi-Vue/blob/0e2d75c23c0d7a1fa85f660f06a59a4dd1ba14c0/ruoyi-framework/src/main/java/com/ruoyi/framework/aspectj/DataScopeAspect.java#L91)。
- **[R2]** [功能权限集合检查](https://github.com/yangzongzhuan/RuoYi-Vue/blob/0e2d75c23c0d7a1fa85f660f06a59a4dd1ba14c0/ruoyi-framework/src/main/java/com/ruoyi/framework/web/service/PermissionService.java#L27)、[Redis LoginUser](https://github.com/yangzongzhuan/RuoYi-Vue/blob/0e2d75c23c0d7a1fa85f660f06a59a4dd1ba14c0/ruoyi-framework/src/main/java/com/ruoyi/framework/web/service/TokenService.java#L62)、[getInfo 刷新](https://github.com/yangzongzhuan/RuoYi-Vue/blob/0e2d75c23c0d7a1fa85f660f06a59a4dd1ba14c0/ruoyi-admin/src/main/java/com/ruoyi/web/controller/system/SysLoginController.java#L72)。
