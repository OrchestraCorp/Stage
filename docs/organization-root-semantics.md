---
title: Stage 组织授权根语义与私有部署边界
status: accepted-design
implementation_status: organization-root-rename-implemented-business-units-pending
decision_date: 2026-10-25
policy_effect: none
---

# Stage 组织授权根语义与私有部署边界

Stage 的私有化部署边界是：**一个部署实例只有一个组织授权根**。这个根是该实例的信任来源、治理配置、授权 epoch、审计边界和数据库归属的治理容器。它是安全与运行边界，不是“一个实例只能登记一个法人公司”的业务主数据表；它也不等同于密码学信任根，认证/签名 trust 和 key 仍分别建模、验证和撤销。

因此，下面三个概念必须分开：

| 概念 | 表达的事实 | 当前状态 |
|---|---|---|
| `organization_root` | 一个 Stage 实例的授权、信任与治理根 | 已落地；数据库至多一行 |
| `organization_unit` | 根下的集团、子公司、事业部、部门等业务组织单元 | 未来按业务需要建设 |
| `authorization_scope` | 某项权限适用的边界，例如部门、项目或资源集合 | 作为授权模型建设；不要求等同组织树 |
| `legal_entity` | 法人、结算主体或合同主体 | 未来按领域需要建设，不由授权根代替 |

一个根可以服务一个简单企业，也可以在明确治理政策下承载一个集团及其多个业务组织单元。是否共用一个根，取决于是否共用数据库、信任来源、治理和运维责任；不能从组织名称或成员数量推导。

## 1. 术语和责任边界

### 1.1 组织授权根

`organization_root` 是 Stage 的唯一组织治理容器。与它关联的可信配置和受控事实决定：

- 当前实例接受哪些认证 profile、issuer、client 和 assurance 条件；
- 哪些主体、成员经历、任职、关系和服务授权属于本实例；
- 当前组织授权 epoch、撤销、审计和 outbox 的一致性边界；
- 这个数据库和部署环境是否仍处于 active 状态。

根被停用时，历史引用保留，身份读取及 P1b.3 人员工作项入口均在组织 guard 后检查当前根状态。公开命令同时重验 profile/trust/外部映射、来源准入、成员和选定任职；旧 fixture 不能替代真实认证。不能通过新建根或客户端选择参数绕过停用、撤销或信任配置，当前读写及结果恢复范围见[P1b.3 记录](reviews/p1b3-human-work-2026-10-25.md)。

根不负责保存完整组织树、法人登记、财务结算、HR 主数据或跨实例全局人员目录。需要这些事实时，由对应领域表或外部系统提供，再通过受控引用参与授权。

### 1.2 组织单元和授权范围

`organization_unit` 表达业务归属，例如集团、子公司、事业部、部门或团队。它可以有父子关系，但父子关系是组织语义，不能自动等价为权限继承。

`authorization_scope` 表达一次授权的适用范围。它可以引用组织单元、项目、资源集合或其他登记对象，也可以是独立的安全边界。scope 的父子关系、继承和 descendants 规则必须由授权策略明确；建立组织单元不会自动产生权限，建立 scope 也不会自动授予角色。

`legal_entity` 如果以后需要，应保存法人名称、登记标识、合同或结算关系，并由领域策略决定哪些主体可以代表它。它不能作为身份根、认证来源或数据库隔离键。

### 1.3 Principal、成员经历和责任身份

Principal 是单个 Stage 部署内稳定的责任主体。一个 human Principal 可以拥有多条成员经历和多个责任身份，例如研发工程成员、项目负责人和知识审阅人。一期人员业务请求选择明确的 assignment，默认不合并全部任职；Agent 只作为受监督工具，不独立入职或任职。

成员经历仍按一次加入/离开周期建模。重新加入创建新的 membership 引用，旧任职和 ACL 不会自动复活。独立 Agent registration/binding/Delegation 从一期后置；技术 workload 认证保持已有事实，不等于独立业务参与者。详见[一期协作模式](people-company-collaboration.md)。

这里的“稳定”只在一个 Stage 部署内成立。两个独立 Stage 实例分别持有自己的 Principal 映射、成员经历、任职、撤销和审计；相同的人可以在两个实例中通过各自受信任的 IdP 关联到两个本地主体，但 Stage 不提供跨实例的全局身份、全局角色或跨实例 token。

## 2. 表结构和兼容命名

迁移 `d6a3c5f8e902` 将根表物理名称从 `tenant` 改为 `organization_root`，并将根列从 `tenant_ref` 改为 `organization_ref`。这是无损重命名，保留原数据、约束、主键和引用。前序迁移 `c4f2a89d1e63` 已负责多根预检和单例约束；重命名迁移沿用该约束，不额外解释法人或组织单元。

以下兼容名称暂时保留：

| 现有名称 | 当前语义 | 处理方式 |
|---|---|---|
| `tenant_*` 表和列 | 部署组织根及其成员、认证绑定、执行 guard 的历史物理名称 | 继续兼容；新公开语义使用 organization |
| `execution_tenant_guard` | 当前组织授权 epoch 的串行化行 | 继续复用；不创建第二套 root guard |
| `tenant_membership` | 根内一次成员经历 | 后续可在无损迁移中改名，不能改变成员经历的生命周期语义 |
| `tenant_authentication_binding` | 根接受的认证 profile、环境和 assurance 约束 | 后续可改名，仍由唯一根决定，不可由请求选择 |

身份 ORM 使用 `OrganizationRootTable`、`OrganizationMembershipTable` 和 `OrganizationAuthenticationBindingTable`，对应用暴露 `organization_ref`。成员和认证绑定仍映射到原 `tenant_*` 物理表/列，避免为了术语收敛重写历史记录；内部执行层的 guard 和审计字段继续兼容。

因此，安全表的复合键在过渡期仍可能使用 `tenant_ref`，但它表示部署组织根，而不是 SaaS 租户选择器。任何新表或新接口应优先使用 `organization_ref`；迁移不能猜测旧 scope 前缀、补默认企业或合并多根数据。

根的单例约束只限制一个部署实例中的授权根数量。它不限制未来在根下登记多个组织单元或法人主体，也不允许客户端通过 query、header 或 body 选择第二根。

离线管理计划的新输入使用 `organization_ref`；根撤销类型为 `organization`。旧 `tenant_ref` / `kind: tenant` 经过明确规范化继续接受，冲突的重复字段拒绝。持久幂等摘要维持既有编码，历史 `revoke.tenant` journal 与审计 JSON 保留原样；对外查询投影统一为 organization 名称。改名不能产生第二次初始化或撤销，也不能使旧未知结果失去恢复入口。

## 3. 部署与 API 规则

- 每个实例使用一套专属数据库和一个固定部署环境；开发、测试、生产分别部署。
- 实例从服务端唯一 `organization_root` 解析组织。身份和业务 API 不接受 `tenant_ref` 或 `organization_ref` 选择参数。
- `/api/v1/identity/me` 只返回服务端确定的 `organization_ref`，不返回“可切换公司列表”。
- `/health` 和 `/ready` 是实例探针，保留根路径；业务 API 使用 `/api/v1`。
- 停用根、认证来源撤销、主体撤销和成员经历终止，都在本根的事务与 epoch 协议内处理。
- 迁移发现历史存在多个根时停止并报告，不自动挑选、合并、删除或把它们解释为组织单元。

如果未来需要集团和子公司共用一套 Stage，先新增组织单元和明确的 scope/关系规则；不重新引入 SaaS 租户选择。如果两个业务边界需要独立信任根、数据库、审计责任或运维控制，则部署两个 Stage，由客户端分别连接。

## 4. Maestro 的多实例边界

Maestro 可以维护多个 Stage endpoint，但每个 endpoint 都是独立的安全会话。连接、登录事务、凭据、主体、成员经历、任职、缓存和恢复地址必须绑定同一个客户端 `connection_id`：

```text
Stage A endpoint → Stage A issuer/profile → Stage A token/session → Stage A Principal
Stage B endpoint → Stage B issuer/profile → Stage B token/session → Stage B Principal
```

Maestro 可以在界面上显式切换实例，不能把 A 的 access token、refresh token、Principal、assignment 或授权结果发送给 B。登录回调、迟到请求和 `COMMIT_OUTCOME_UNKNOWN` 恢复都回到发起它们的实例；切换不等于退出其他实例。Stage 不需要为了支持这个客户端体验而建立跨企业全局身份表。

具体连接对象、切换不变量和共同验收见[Maestro 多 Stage 连接与切换合同](maestro-multi-stage-connections.md)。

跨实例协作如果以后出现，应通过两个实例各自签发、各自验证的受控命令或交换协议实现，并在两边留下本地审计；它不等于跨实例共享身份或原子事务。

## 5. 当前落地和未建设范围

本轮已经完成根表和根引用的无损重命名，继续保留兼容的 `tenant_*` 物理字段；公开身份 API 的组织字段使用 `organization_ref`，客户端不能选择组织。当前没有实现：

- 组织单元、集团/子公司业务树和法人登记；
- 多实例统一人员目录、跨实例账号合并或跨实例角色；
- Stage 端的公司切换、SaaS 租户开通和共享数据库控制面；Maestro 的多实例连接切换属于客户端合同，尚待本地实现和验收；
- 完整个人资料 CRUD；首个组织级人员目录和有限任职管理已在独立管理切片交付。

人员角色版本、scope/resource 已有内部切片，业务领域关系按[身份表结构设计](identity-schema-design.md)继续推进。独立 Agent 委托和 ServiceGrant 属于后置候选，不构成一期门槛；本文不宣称完整协作业务已经实现。
