---
title: Stage RBAC、ABAC 与 ReBAC 授权模型及 z-pool 迁移方案
date: 2026-10-19
version: 0.1
status: historical-proposal-superseded
classification: internal
policy_effect: none
implementation_status: historical-snapshot-not-current
---

# Stage RBAC、ABAC 与 ReBAC 授权模型及 z-pool 迁移方案

> **历史授权提案，不作为当前授权实现或阶段验收依据。** 保留 2026-10-19 的设计推导与原阶段编号；本文 Phase 1～5 不对应现行 P1～P5。现行约束见[身份与授权](docs/identity-and-authorization.md)、[表结构规格](docs/identity-schema-design.md)及[一期协作边界](docs/people-company-collaboration.md)，实现覆盖见[P1b 身份覆盖矩阵](docs/reviews/p1b-identity-coverage-2026-10-25.md)。
>
> **第 1 节的“角色授予 ∪ 关系授予 ∪ 有效委托授予”不是当前执行规则。** 当前人员命令须同时满足：真实人员身份与有效成员资格、明确单任职的固定角色版本 action、授权 scope、该 action 要求的对象关系以及 ABAC 条件。`owner_of`/`viewer_of` 不单独补足缺失的角色 action，也不合并不同任职的权限。独立 Agent/Delegation 和通用 ServiceGrant 后置；保留的相关模型是历史候选，不要求一期实现。

## 1. 结论

Stage 不应在 RBAC、ABAC、ReBAC 之间三选一，而应让三者承担不同职责：

```text
RBAC  = 主体拥有什么基础能力
ReBAC = 主体与业务对象之间是什么关系
ABAC  = 当前条件是否允许这次具体动作
```

最终判断可以写成：

```text
基础能力
  = 角色授予 ∪ 关系授予 ∪ 有效委托授予

允许执行
  = 基础能力包含 action
  ∧ ReBAC 关系满足资源范围
  ∧ ABAC 约束全部通过
  ∧ 对象版本、业务状态、审批和租约有效
```

这里的“并集”借鉴 z-pool 的纯正向权限组合；ABAC 的约束不是偷偷从权限集合扣除，而是单独产生可解释的拒绝、待审批或未知结果。

## 2. 三种模型各自解决什么问题

### 2.1 RBAC：稳定的能力基线

RBAC 适合表达稳定、可审阅的能力集合：

- `engineering-member` 可以提交工程任务交付。
- `finance-auditor` 可以查看财务审批并执行复核。
- `department-owner` 可以管理本部门的岗位安排。
- `maestro-knowledge-steward` 可以提出知识发布 MR。

RBAC 不应该直接表达“某个人当前能否操作某一条任务”，也不应该把所有企业差异都塞进角色名称。

角色只授予能力点，例如：

```text
task.view
task.commit.create
task.delivery.submit
task.acceptance.review
knowledge.candidate.propose
repository.mr.propose
```

业务代码只调用能力点守卫，不写 `if role == admin`。角色定义和权限点可以先放在代码注册表中，稳定后由 ScoreBook 发布版本承载。

### 2.2 ReBAC：关系决定作用对象

ReBAC 适合表达主体与业务对象之间的关系：

```text
alice      --member_of-->       engineering
alice      --assignee_of-->     work_item:123
bob        --owner_of-->        department:engineering
carol      --reviewer_of-->     delivery:456
agent-x    --delegated_by-->    alice
team-a     --participant_of-->  initiative:alpha
```

关系元组建议使用稳定结构：

```text
subject_type / subject_id
relation
resource_type / resource_id
valid_from / expires_at
source_ref / policy_version
status
```

首版使用关系表和有界查询，不立即引入独立图数据库。关系路径深度默认限制在 0～2 层，避免一次授权请求变成不可控的图遍历。

ReBAC 解决“这个人和这个对象是否存在允许的关系”，不直接决定金额、时间窗口、数据分类或审批条件。

### 2.3 ABAC：当前上下文的条件约束

ABAC 负责判断一次动作的动态条件：

- 任职仍然有效。
- 委托没有过期或被撤销。
- 任务处于允许状态，且对象版本匹配。
- 数据分类不超过当前主体的可见上限。
- 当前时间在值班、工作窗口或 SLA 范围内。
- 审批人不能同时是申请人。
- 金额、环境、地区或风险等级在允许范围内。
- GitLab 项目实际权限已经核验，而不是只存在一条期望配置。

首版不要提供任意 Python 表达式或通用策略语言。先提供有限、类型明确的谓词：

```text
subject.active
assignment.valid
delegation.valid
relation.exists
resource.classification_at_most
resource.status_is
object.version_is
time.within
approval.satisfied
separation_of_duties.pass
external_acl.verified
```

以后确有需要，再把这些谓词编译到受控 DSL；不能让 Agent 直接生成可执行策略代码。

## 3. Stage 的授权上下文

z-pool 的 `AuthContext` 是很好的起点，但 Stage 需要把“当前主体”扩展为“当前动作上下文”：

```python
@dataclass(frozen=True)
class AuthorizationContext:
    member_id: str
    acting_principal_id: str
    role_assignment_ids: tuple[str, ...]
    agent_id: str | None
    delegation_id: str | None
    organization_scope: tuple[str, ...]
    task_id: str | None
    resource_ref: str
    action: str
    object_version: int | None
    policy_release_id: str
    evaluated_at: datetime
    request_id: str
```

必须明确当前请求使用哪项任职、哪个 Agent、哪份委托、哪个任务和哪个策略版本。不能把一个人的多个岗位权限自动合成一张“万能权限表”。

## 4. 有效授权判定流程

```text
1. 认证 principal，确认账号仍有效
2. 解析 acting principal：本人、Agent 或服务主体
3. 读取当前任职和有效角色授予（RBAC）
4. 解析主体与目标对象的关系（ReBAC）
5. 读取有效委托、任务范围和数据范围
6. 执行固定属性约束（ABAC）
7. 检查对象版本、业务状态、审批、租约和外部 ACL
8. 返回 Decision，并写入必要审计/事件
9. 业务命令在同一事务中再次核验关键条件并提交
```

授权检查不是业务事务的替代品。读操作可以使用经过短期缓存的决策；领取、审批、撤销、权限变更和外部副作用必须在实际命令事务中重新检查。

## 5. Decision 结果结构

不要只返回布尔值，建议返回：

```python
@dataclass(frozen=True)
class AuthorizationDecision:
    outcome: Literal[
        "allowed",
        "denied",
        "needs_approval",
        "needs_delegation",
        "expired",
        "unknown",
    ]
    action: str
    subject_ref: str
    resource_ref: str
    matched_roles: tuple[str, ...]
    matched_relations: tuple[str, ...]
    failed_constraints: tuple[str, ...]
    policy_release_id: str
    evaluated_at: datetime
    decision_id: str
```

`denied`、`expired`、`unknown` 和 `needs_approval` 不能共用一个 403。对用户可以统一展示，但 Stage 内部要保留不同原因，供 Maestro、审计和接管流程使用。

## 6. 数据模型建议

首版不需要完整通用授权数据库，先使用以下最小对象：

| 对象 | 作用 | 来源 |
|---|---|---|
| `principal` | 人、Agent、服务主体 | Stage / Virtuoso / IdP 引用 |
| `role_definition` | 角色和能力点集合 | ScoreBook 发布版本或代码注册表 |
| `role_assignment` | 当前成员在某组织/岗位上的任职 | Stage |
| `relation_tuple` | 主体与对象的关系 | Stage，来源可指向任务/组织/委托 |
| `delegation` | 人对 Agent 或替补主体的限时委托 | Stage |
| `policy_release` | 已加载的规则/能力版本 | ScoreBook + Stage 激活记录 |
| `authorization_decision` | 高风险判定和失败原因 | Stage 审计 |
| `revocation` | 委托、角色、关系或外部权限的撤销 | Stage + 外部回执 |

属性值不应把所有人员和业务数据复制到授权表中。ABAC 读取必要属性的权威引用，并保存评估时的版本或快照引用。

## 7. 与 z-pool-v2 的代码对应关系

### 7.1 可以直接沿用的结构

| z-pool-v2 | Stage 迁移用途 |
|---|---|
| `app/core/permission.py` | 权限点、角色权限集合、纯并集、`ensure_permission()`、SoD 提示 |
| `AuthContext` in `app/core/deps.py` | 扩展为 Stage `AuthorizationContext` |
| `require_permission()` | 作为基础能力守卫，增加关系和属性决策入口 |
| `scopes_of()` | 保留“业务能力 scope”和前端入口 scope 分离的思想 |
| `app/core/enums.py` | 状态值稳定、含义表一一对应、禁止复用旧值 |
| `app/core/audit.py` | 统一操作人、before/after、动作码和理由快照 |
| `app/core/wallet.py` | 借鉴行锁、唯一幂等键、版本号和不变式检查 |
| `app/gateway/state.py` | 借鉴热运行态、原子 Lua、TTL 和未知/恢复状态；不复制资源池 key |
| `app/gateway/snapshot.py` | 借鉴事务 outbox、投影失效和“通知不是事实” |
| `contracts/openapi.yaml` | 延续契约先行与代码/契约漂移检查 |
| `tests/core/test_permission.py` | 扩展为 RBAC、ReBAC、ABAC 三层判定测试 |

### 7.2 不应直接复制的部分

- `supplier`、`supplier_member`、`account`、`channel`、`proxy` 等是 z-pool 业务实体，不是 Stage 的组织模型。
- `admin` / `ops` 角色不能直接成为 Stage 的通用角色体系。
- `active_supplier_id` 不能直接类比成“当前组织空间”；Stage 需要组织、任务、数据和资源多种 scope。
- z-pool 的控制面 HTTP 200 业务错误契约不必成为 Stage API 契约。
- Redis 运行态只用于短期租约、并发和限流，不作为任务、承诺或授权的长期权威。
- z-pool 的状态值、财务精度和资源池错误码不进入 Stage。

## 8. 推荐的代码目录

为了让熟悉 z-pool 的团队容易上手，Stage 可以保留相近的扁平分层，但把授权单独形成领域：

```text
stage/
├── app/
│   ├── authz/
│   │   ├── permissions.py       RBAC 能力点和角色基线
│   │   ├── relations.py        ReBAC 关系解析
│   │   ├── attributes.py       ABAC 固定谓词
│   │   ├── context.py          AuthorizationContext
│   │   ├── decision.py          AuthorizationDecision
│   │   ├── guards.py            FastAPI/命令层守卫
│   │   └── revocation.py        撤销和缓存失效
│   ├── core/
│   │   ├── audit.py
│   │   ├── idempotency.py
│   │   ├── outbox.py
│   │   ├── errors.py
│   │   └── time.py
│   ├── models/
│   │   ├── identity.py
│   │   ├── organization.py
│   │   ├── authorization.py
│   │   └── work.py
│   ├── features/
│   │   ├── work/
│   │   ├── approvals/
│   │   ├── deliveries/
│   │   └── knowledge/
│   └── integrations/
│       └── gitlab/
├── contracts/
├── migrations/
├── tests/
│   ├── authz/
│   │   ├── test_rbac.py
│   │   ├── test_rebac.py
│   │   ├── test_abac.py
│   │   └── test_composed_decisions.py
│   ├── commands/
│   └── integrations/
└── scripts/
```

这不是要求 Stage 复制 z-pool 的业务目录，而是保留团队已经熟悉的 `app/core`、`app/models`、`app/features`、`contracts`、`tests` 组织方式。

## 9. 分阶段实现路线

### Phase 1：RBAC 基线

- 搬入 z-pool 的权限点、角色集合、`ensure_permission()` 和权限测试风格。
- 将主体成员改成 `principal + role_assignment + organization_scope`。
- 不改变现有业务命令的能力点接口。

### Phase 2：ReBAC 关系

- 增加 `relation_tuple` 和关系查询器。
- 先支持 `owner`、`member`、`assignee`、`reviewer`、`participant`、`delegated_by`。
- 用有界 SQL 查询，不引入图数据库。
- 将关系结果作为能力范围的来源之一。

### Phase 3：ABAC 固定约束

- 增加任职有效期、任务状态、数据分类、对象版本、时间窗口、审批和 SoD。
- 每个谓词有独立单测和稳定失败码。
- 不允许直接执行任意策略表达式。

### Phase 4：统一授权决策

- 把 RBAC、ReBAC、ABAC 合并到 `authorize(context)`。
- 记录命中的角色、关系、失败约束、策略版本和评估时间。
- 读操作可缓存，写操作和高影响动作重新核验。

### Phase 5：Maestro 与蓝图

- Maestro 生成角色、关系、属性约束和工作流组合蓝图。
- Stage 执行 schema、依赖、迁移、冲突和权限检查。
- 通过 plan/review/apply/verify 生效，不让 Maestro 直接改运行数据库。

## 10. 必须覆盖的测试

- RBAC 多角色并集不会隐藏扣除权限。
- ReBAC 关系失效、过期或被撤销后立即失去对象范围。
- ABAC 条件失败不会被角色权限绕过。
- Agent 只有在有效 `delegation` 内代表员工行动。
- 同一成员兼任多个岗位时，不自动合并不兼容权限。
- 审批人不能审批自己的申请，除非企业明确配置补偿控制。
- 任务版本、时间租约或外部 ACL 过期时，写命令被阻断。
- `unknown` 不会被当作成功，也不会被无条件重试。
- 授权决策和业务事务的审计能关联同一 `request_id`、`decision_id` 和 `object_version`。
- 受限部门、仓库、附件、搜索和投影不会因拥有任务读取权而越界。

## 11. 首版取舍

Stage 首版不做通用授权语言、任意 ABAC DSL、无限深关系图、独立图数据库、全局权限缓存或动态策略代码加载。

首版做一条完整链路：

```text
RBAC 能力点
→ ReBAC assignee/reviewer/delegated_by
→ ABAC 有效期/任务状态/数据分类/对象版本
→ ClaimWork / SubmitDelivery / AcceptDelivery
→ 审计、outbox、GitLab MR 和接管
```

这样内部开发可以先沿用 z-pool 熟悉的权限点、依赖注入、模型、服务、契约和测试习惯，再逐步加关系与属性，而不会一开始重写成一套陌生的策略平台。

