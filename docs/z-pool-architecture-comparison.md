---
title: Stage 与 Z Pool 的代码架构历史对照（2026-10-24）
status: historical-research-record
policy_effect: none
review_date: 2026-10-24
---

# Stage 与 Z Pool 的代码架构历史对照（2026-10-24）

> **历史静态评审，只描述表中固定版本和当时工作区。** 保留文件数、代码行数、表数、路由数和原源码定位，不重新计算或冒充当前实现。下文“当前”“尚未”和目标路线均指本次研究时点；最新范围见[当前代码架构](current-code-architecture.md)及[P1b 收尾证据](reviews/p1b-closeout-2026-10-25.md)。Core、真实人员认证、单任职授权和最小 HTTP 编辑现已落地，完整人员管理/MFA、WASM、签名和持久工作流仍需独立验收。
>
> Stage 的现行产品边界是 PG＋Redis、企业私有单组织根与“人员—公司—人员”；本文 MySQL/多租户/Agent 委托等历史比较不构成当前实施要求，参见[存储策略](storage-strategy.md)、[人员协作边界](people-company-collaboration.md)与[路线图](roadmap.md)。

结论分三层：**技术栈与工程习惯接近；现有业务实现深度是 Z Pool 明显领先；Stage 的目标架构已经转向可组合组织运行平台，与 Z Pool 的固定业务后端有较大差异。**不能用 Stage 新文档里的 Core、真实身份协议、WASM 和签名准入代表已经实现的优势。

## 1. 本轮依据

| 对象 | 基线与范围 |
|---|---|
| Z Pool | `eebab092b715258a636f387f498c834a84de39d1`，跟旧参考记录一致；本轮受跟踪文件无改动 |
| Z Pool 实际位置 | `/path/to/z-pool-backend-v2`；原本地 Documents 路径已不在，只按需下载云占位源码供读取，未搬迁或改写项目 |
| Stage | `271d175546901832fd699bc570745901a617b03b`；新增改动主要为设计文档，app 实现未进入新基座开发 |
| 读取范围 | 完整 app Python 文件清单/AST，重点读取入口、身份权限、数据库、业务事务、适配器、网关和调度；结合 Stage 新设计 |
| 验证边界 | 静态源码核对；未启动服务、执行测试、连接真实数据库或验证生产可靠性 |

逐文件摘要、统计口径和 Git 状态见[本轮证据](reviews/z-pool-comparison-2026-10-24.json)。以下表中的源码路径以对应项目根目录为起点，行号用于固定本轮定位。

## 2. 当前代码规模与功能状态

| 静态指标 | Z Pool | Stage |
|---|---:|---:|
| app 下 Python 文件 | 172 | 32 |
| app 物理行数，含注释/空行 | 28,414 | 1,236 |
| ORM `__tablename__` 定义 | 33 | 4 |
| 路由方法装饰器声明 | 131 | 1 |
| `test_*.py` 文件 | 58 | 14 |

这些数量只说明实现规模，不是质量评分、测试通过数或项目完成比例。路由统计是源码声明而非运行时路由探测；Stage 实际业务 router 为空，唯一声明端点为 `/health`。

Z Pool 已有供应商、渠道、模型、账号资源、代理分配、用量、台账、钱包、提现、调账、审计等实体与服务；Stage 当前原始业务表仍只有 principal、role_assignment、relation_tuple、work_item，但本轮已新增 execution/audit/outbox/delivery 持久模型及内部执行链。Stage 的三种 work command 仍是输入 dataclass，真实身份、WASM、任务领域和 HTTP 接入尚未实现。

## 3. 哪些仍然接近

- 两者均采用 Python、FastAPI、Pydantic、SQLAlchemy async、Alembic 和 uv/锁文件，沿用 app/core、features、models、API、contracts、tests 等工程组织。
- 都强调权限点、数据库约束、稳定状态/错误码、明确事务、审计与契约检查；这些方法在 Z Pool 已有具体业务实现，Stage 正在抽象成通用执行契约。
- 都有明确的入口生命周期和异步会话。Z Pool 在 `app/core/db.py:11` 模块级创建 engine / `AsyncSessionLocal`；Stage 在 `app/main.py:18` 的 lifespan 创建 engine/session factory，放入 app.state 并在退出释放。Stage 这一处更便于隔离实例与测试，但还不是组件依赖运行时。
- Z Pool 并非没有扩展设计：`app/gateway/adapters/contracts.py:102` 定义请求、错误和额度 Protocol，`registry.py:43` 显式注册、拒绝重复 key，`:147` 未知 key 返回 None。它是专用渠道适配层，尚不是带 owner/generation/drain 的通用组件内核。

## 4. 按架构职责比较

| 维度 | Z Pool 已实现 | Stage 当前实现 | Stage 目标 |
|---|---|---|---|
| 产品问题 | 供应商资源池、网关与结算的具体业务 | 组织协作底板与纯授权规则 | AI驱动、可验证、分布式工作协作底座 |
| 模块组织 | router/service/schema/models；专用适配器注册；模块直接调用共享基础设施 | 类似目录，业务服务尚少 | 能力目录、稳定接口、组件依赖及实例生命周期 |
| 部署与运行 | 控制面、网关、scheduler三个进程；各入口手工装配/清理 | 单FastAPI生命周期与数据库工厂 | 单worker可信宿主＋受控WASM runner；后续多节点与版本协调 |
| 身份认证 | 本地账号、密码、JWT；每请求读取用户和成员状态 | 尚无登录/真实协议adapter | 外部IdP/受控workload、主体映射、任职、委托、凭据/权限撤销 |
| 授权 | 全局角色与当前供应商成员权限明确并集；集中依赖守卫 | 纯RBAC/ReBAC/ABAC、scope/有效期/版本与Agent委托收窄 | 可信事实加载＋每次查询/命令执行强制授权＋撤销竞争协议 |
| 流程 | 业务service与定时job中的固定路径 | work输入结构，无流程运行器 | 自研蓝图/计划/持久实例，分支、等待、恢复与迁移 |
| 事务及副作用 | 业务行锁、唯一键、状态日志、审计、routing_outbox已有实现 | Session辅助、数据结构；无持久CommandExecution/outbox worker | 通用执行/attempt/幂等/未知结果及同事务审计/outbox |
| 安全扩展与发布 | 可信Python代码、显式注册、镜像部署与数据库迁移 | 尚无WASM或包发布模块 | 固定WASM ABI、进程预算、批准签名清单、当前准入与激活 |
| 数据兼容 | MySQL导向；SQLite用于部分单测 | MySQL/PostgreSQL/SQLite方言与UTC/精确字符串类型底板 | 按目标库进行真实锁/迁移与授权事务验证 |

Stage 的当前授权判定比 Z Pool 的角色集合计算覆盖更多组织条件，但其输入仍要求可信事实。ORM 任职表尚未具备纯授权Context要求的完整组织/任职字段，也没有成员、外部身份与委托的完整持久模型，不能把纯函数测试当成已接通企业IAM。

来源：Z Pool `app/core/deps.py:44,72,127,171`、`app/core/permission.py:220`；Stage [decision.py](../app/authz/decision.py) 第31行、[context.py](../app/authz/context.py) 第55行、[authorization.py](../app/models/authorization.py) 第10行。

## 5. Z Pool 值得继承的工程资产

| 工程经验 | 原码证据 | Stage 应怎样使用 |
|---|---|---|
| 主体隔离由统一入口提供 | `app/core/deps.py:127` 核验当前供应商选择，`:181` 返回受权主体 | 发展为可信tenant/assignment/delegation loader，不能直接复制供应商模型 |
| 业务不变量落在事务入口 | `app/core/assignment.py:263,411` 分配与绑定；`wallet.py:89` 钱包原语 | 能力handler有明确领域不变量，数据库唯一键与锁兜底 |
| 审计与业务同事务 | `app/core/audit.py:59` 将稳定动作、before/after与操作者挂入当前Session | 扩展为actor/effective subject/批准/执行的可追溯关联 |
| 自动机制尊重人工状态 | `app/core/failover.py:64` 自动换绑检查限制与冷却 | Agent/自动恢复不能绕过用户暂停、当前授权或组织约束 |
| 运行资源有明确收尾 | `app/gateway/app.py:67` 管writer、outbox task及HTTP/Redis/DB关闭 | 将手工生命周期转成自研Core的owner、lease与drain契约 |
| 热状态与业务事实分工 | `app/gateway/state.py:119` Redis原子占槽/额度；DB保存长期事实 | 按需求设计，不把所有业务状态搬入Redis或把缓存当权威 |
| 定时维护与恢复有实现 | `app/scheduler/jobs.py:100` 起、`runner.py:48` 显式注册任务 | 复用异常边界、回执、幂等和恢复思路；不是通用持久工作流引擎 |

Z Pool 的供应商、模型路由、代理、价格与钱包是它的产品域，Stage 不应为“复用”而继承这些实体或MaaS职责。可迁移的重点是经过重新验证的工程方法与契约，而不是整套业务代码。

## 6. 对原参考记录的两处纠正

**钱包写入不是全仓唯一入口。**`app/core/wallet.py:89` 的 `move()` 提供行锁、幂等流水和版本快照，但 `app/core/settlement.py:238` 的 `_credit_wallet()` 也直接修改余额/version，并未调用该函数。因此旧参考记录“所有改余额动作都经过move”过于绝对。这里不据静态阅读断言真实资金已出现问题；并发正确性需要单独测试。

**DB与Redis不是一个原子事务。**`app/gateway/lifecycle.py:57` 起处理数据库封禁、释放绑定、日志及outbox，`:110` 起的Redis重置允许失败。不能从文件注释推导数据库和缓存同时原子提交。

另一个成熟度边界是认证撤销：Z Pool `auth/service.py:101` 与 `auth/api.py:25` 明确改密/登出不提供精确JWT会话失效。Stage的目标扩展了认证协议、当前业务授权和撤销模型，但这些目标尚待实现与验证。

## 7. 对“差距多大”的判断

- **技术栈：小。**已有Python/数据库/契约工具链可继续使用。
- **工程组织：中等。**现有目录结构仍相似；目标要把生命周期、能力、身份、准入和领域执行拆成稳定边界。
- **业务实现深度：大，当前Z Pool领先。**Stage新设计还没有成为可运行产品，不应因词汇更完整就称工程更成熟。
- **目标抽象与安全模型：大。**Stage从具体业务应用走向可组合组织运行平台，新增能力目录、当前身份/授权、签名制品、隔离计算和持久流程协议。

当前合理路线是保留可靠工程基础，用P1 Core＋身份＋WASM最小资格评估闭环证明新的执行契约，再扩展任务业务与通用流程。自研框架带来的并发、恢复、版本兼容和安全维护成本必须通过验收控制，不能靠增加目录或规范名称抵消。

具体迁入范围、已有基础与改造条件见[工程能力复用计划](z-pool-reuse-plan.md)，不以增加文件/接口数量或复制旧业务为目标。

本轮只补充比较记录并纠正Stage参考文档，没有修改Z Pool或Stage应用代码，也没有运行原项目或新基座测试。
