---
title: Stage 可组合运行时与业务执行架构
status: design-proposal
implementation_status: p1a-implemented-p1b-p1d-pending
policy_effect: none
research_date: 2026-10-23
specification: stage-runtime-core-v1
---

# Stage 可组合运行时与业务执行架构

企业私有化部署是当前产品边界：一套实例一个组织授权根、专属数据库和固定环境。根是信任与治理边界，不等同于一个法人公司；现有 tenant 类型保留为组织根的内部兼容名称。没有多租户控制面、组织选择或 SaaS 共享数据库目标；集团、子公司、部门等业务组织单元以后按需建模。HTTP 业务合同使用 `/api/v1`，见[组织授权根语义](organization-root-semantics.md)和[部署与版本约定](private-deployment-api-v1.md)。

## 1. 目标与现状

Stage 使用 Python 实现可组合的组织协作服务。组件通过声明依赖取得能力，运行时记录资源归属、协调启停与依赖替换；业务命令通过可信身份、组织范围、授权和事务改变持久事实。两类机制各有验证标准，不相互替代。

2026-10-24 明确自研方向：Stage 从初期实现自己的组件运行时与业务流程运行器，不采用 Graphon 作为产品依赖。两者共享能力目录、身份上下文、版本绑定与结果契约，分别管理资源生命周期与持久流程状态；Graphon / Dify 保留为研究对照。该方向服务于 OrchestraCorp 的 AI 驱动、安全可靠的分布式工作协作愿景，详见[方向与边界](vision-and-runtime-direction.md)。

本文件描述目标架构。当前已实现纯授权、内部命令/审计/outbox、P1a RuntimeHost 与 PG/Redis providers，命令和投递可经可信组件装配；HTTP 提供 `/health` 和 `/ready`。真实身份 adapter、WASM、任务业务和生产 sink 尚待后续阶段，证据见[P1a 验证](reviews/p1a-validation-2026-10-25.md)。

2026-10-24 进一步按基座优先重排 P1：自研 Core、真实身份协议、当前授权、受控 WASM 和签名准入先贯通，完整任务业务后置。共同对象见[核心对象契约](core-object-contracts.md)，核对和纠正见[设计复核](design-review-2026-10-24.md)。

2026-10-25 用户确定 PostgreSQL＋Redis 长期组合，本轮工程基线采用 PostgreSQL 17 为唯一主库、Redis 8 为辅助缓存/短期协调/唤醒，取消 MySQL 生产兼容要求。存储策略与 Core 的 P1a 接入已验证；真实身份、WASM 与业务使用方仍按后续阶段实施。

P1a 的接口、两张运行图、关闭阶段、事件调用 pin 和 readiness 以[框架实施规格](framework-foundation.md)为准；C1–C8 的 34 项加 S1 的 12 项已通过 Python 映射验收，固定 Cordis 共享行为与差异另行留证。

设计首先服务于模块化后端。第一期采用单事件循环、单 ASGI worker 的可信 Stage 宿主，外加受控 WASM runner 子进程；宿主同时监督 HTTP 与 outbox worker。CPU 编译/执行在独立受限进程，不阻塞宿主事件循环；这种执行隔离不等于多个 ASGI worker 或已完成分布式协调。CLI 使用同一应用服务契约。

## 2. 总体架构

![Stage 一期目标与长期扩展](diagrams/stage-architecture.png)

[SVG 矢量版](diagrams/stage-architecture.svg) · [Mermaid 源文件](diagrams/stage-architecture.mmd) · [图例与复现](diagrams/README.md)

图的实线表示请求、持久状态或投递路径；虚线表示装配、资源管理或后续集成。这是整个一期的目标图：Core 与存储装配已完成，身份、发布准入与 WASM 仍待实现。默认进程的实际装配范围见[当前代码架构](current-code-architecture.md)。

| 层 | 第一期开出的接口与职责 | 生命周期 |
|---|---|---|
| 入口适配 | HTTP 输入校验、CLI 请求、受管 worker 的服务身份；将外部请求转换为命令 envelope | 宿主或请求 |
| 可信上下文 | 验证认证证据，加载企业成员、选定任职、委托与读者范围；生成 ExecutionContext | 每请求 / 每消息尝试 |
| 命令注册与执行 | 注册带版本的命令；获取当前有效 handler/provider 租约；事务内重验并执行领域不变式 | 注册属组件，执行属请求 |
| 领域能力 | 一期提供身份/授权、制品批准与资格评估演示；任务、承诺、容量、交付与验收在后续切片扩展 | 组件提供能力，数据长期存在 |
| WASM 宿主与准入 | 固定签名模块、能力与ABI、预算、独立runner、输入输出与回执核验 | provider 管runner；每次调用独立实例 |
| 存储与外部适配 | UoWFactory、时钟、身份适配、通知 sink；定义最小接口并注入实现 | provider / 事务 |
| 运行时 | Context、依赖图、Fiber 状态、effect、任务监督、事件、故障与就绪报告 | 整个进程及组件实例 |

FastAPI lifespan 持有根 RuntimeHost，HTTP API 在启动时定义。路由调用命令注册表；组件不可用时返回明确的 unavailable。第一期不动态增删 FastAPI router，也不把修改 OpenAPI 缓存作为插件热卸载机制。该选择将 API 兼容性与实现组件的启停分开管理。[S7]

身份模块一期包含真实协议 adapter 对隔离测试 IdP 的验证，fixture 仅用于辅助测试；见[身份与授权](identity-and-authorization.md)。WASM 宿主一期采用固定、有限纯计算接口，无任意业务hostcall，具体ABI与预算提案见[WASM 设计](wasm-host-design.md)。认证、包验签和计算结束均不能替代提交前的当前授权核验。

后续 Workflow Runtime 由组件内核管理启动、停止和资源，但流程实例保存在持久层。每个步骤从固定计划解析能力版本，取得短期 provider 租约，经过统一查询/命令入口后释放；人工等待不长期占有租约或事务。实例保持旧定义及实现版本的引用，防止未完成流程依赖被回收；组件卸载不会删除实例或撤销已提交业务事实。

## 3. 两种上下文与一个授权投影

早期提案把 provider bindings、用户、任务版本和委托放进同一个 RuntimeContext。这里明确拆分，以防长期共享组件保存“当前用户”或“当前租户”。

| 对象 | 内容与作用 | 不应承担的职责 |
|---|---|---|
| `ComponentContext` | 服务键、realm、intercept、组件 owner、配置、注册句柄、代次、资源清单 | 不存当前登录主体，不提供可任意使用的全局业务权限 |
| `ExecutionContext` | 应用层 request / execution ID、认证主体、tenant、任职 / 委托引用、deadline、调用来源；组合 Core 的 CallScope | 不属于 Core 类型；不允许调用方变更插件树，身份事实由服务端构造 |
| `AuthorizationContext` | 现有纯判定引擎使用的可信事实：主体、有效任职、对象范围、关系、分类、当前 / 预期版本 | 不是客户端 DTO，也不是可持久复用的允许令牌 |

ExecutionContext 中的引用用于定位权威记录，写命令在事务内重读关键事实，再构造 AuthorizationContext。入队或请求开始时的允许结果，不能替代写入时的撤销、到期和版本核验。

`ContextVar` 只用于 trace ID、日志相关信息等隐式诊断数据，不作为授权来源。创建异步任务可能复制调用时的上下文，不能借它实现跨任务权限同步；worker 每次执行都要重新构造 ExecutionContext。[S5]

服务 realm 决定“这个组件拿到哪个 db/logger/adapter”；主体、任职和 scope 决定“这次动作能读写哪些记录”。当前产品一套 Stage/数据库只服务一个组织授权根与环境，内部 tenant 字段由服务端唯一根填入。根下的组织单元、部门与项目通过领域模型和 scope 隔离，不创建第二根；独立部署仍不能替代根内授权检查。

## 4. Runtime Core 的职责

Core 是小型独立基础库，建议位于 `app/runtime/`，不导入 FastAPI Request、具体 ORM 模型或组织角色名。

Core 定义无身份字段的 ServiceLease/CallScope，不导入 execution/authz。owner 树表达结构归属，服务依赖图表达 committed binding；关闭同时协调两者。命令注册表属于应用执行层，借用 Core 的 owned registration，不把业务 schema 放进 registry。

| 对象 | 设计约束 |
|---|---|
| `RuntimeHost` | 启动一份版本明确的组合，暴露 readiness、诊断和关闭结果；拥有根实例 |
| `ServiceKey[T]` | 稳定接口键与版本；Protocol 提供开发期类型约束，运行时仍检查配置、接口版本与注册冲突 |
| `PluginSpec` / `Fiber` | 分开插件定义与运行实例；声明 required/provided services；记录激活代次与状态 |
| `ServiceBinding` | `(realm, key, provider identity, generation, registration_id)`；消费者激活时保存 committed view，同代次重登不复用 ID |
| `EffectScope` | 记录原子资源建立与清理，失败时释放已登记资源，禁止退出期间再产生无主资源 |
| owned registration / `EventBus` | 贡献有 owner 和注册代次；应用 CommandRegistry 复用此机制，旧 disposer 不能删除新 handler |
| 请求 / 任务租约 | 在途命令固定所用依赖；drain 后禁止取得旧代次新租约，原租约释放前不能关资源 |

Python 层用显式 `ctx.require(key)`、返回 disposer 的注册接口、异步上下文管理器和结构化任务管理实现，不要求模拟全部 JS Proxy 语法。调用共享服务创建资源时，服务接收明确的 owner 或使用绑定到调用者的 wrapper；不能修改共享 provider 的 `self.ctx` 来假装调用方作用域。[S1][L2]

第一期支持受控程序化 `load / retire / replace / await_ready / close`。Python 组件列表来自受信任的 composition root；领域用户没有装配 API。受控导入的标准 WASM 模块经签名准入后只能在独立 runner 的限定 profile 中执行；不得把上传模块作为宿主 Python 插件加载。

每个组件有自己的任务域和监督策略。可选通知组件失败只使其及其依赖者不可用；数据库或核心命令组件失败则阻止新的业务接入。不能用一个全局 TaskGroup 同时托管所有组件，又声称任意子任务失败不会影响其他模块。取消、超时和清理失败必须进入可观察结果。[S3]

TaskGroup 正常退出会等待任务，不会自动停止无限循环 worker。关闭协议必须先停止接入，再向全部受影响 owner/child/任务发停止信号，等待旧调用和消费者收敛后才清理依赖；停止信号不能藏在 drain 之后的 disposer。runtime 不得持有全局变更锁等待插件代码或 cleanup；provider 的 drain 只等待旧代次释放，不等待 consumer 新代次就绪。已存在 committed pin 允许 cleanup 完成旧 DB 记账，不能借此接收新业务。

### 4.1 换代与关闭

```text
记录目标变更与旧代次
→ 在任何 await 前关闭目标与受影响依赖闭包的新接入
→ 向受影响消费者、children、后台任务发停止/取消信号
→ 等待旧 consumer cleanup、已选事件回调和调用租约释放
→ 清理旧 provider 资源，收集全部清理结果
→ 默认 restart/replace 在 drain 与清理全部成功后激活新代次
→ 发布为可用，恢复受影响的接入
```

清理失败进入 `FAILED(phase=cleanup)`，保留错误、blocked/未完成资源并拒绝新接入；独立安全分支继续，不能越过未收敛依赖强行销毁资源。不自动沿成功路径激活该 owner 的新代次。恢复必须显式触发并确认资源已经收敛；重复 close 共享同一操作，等待者取消不终止它，自等待改用非阻塞 request_close。

同一 Fiber 的加载与卸载不会并发跨代执行；过期 await 结果不能发布成当前 ACTIVE。consumer 清理时仍读取其旧 committed view，而非新的 registry 值。

不同 Fiber 的 P2 独立挂载属于另一条受控路径：P1 的公开登记已撤销、槽释放后，P2 可以可用；旧 consumer 仍 pin P1，只有自身 cleanup 成功才可重新绑定。C4-02 验证该路径，不与上面的默认串行 replace 混同，也不据 P2 可用推断 P1 已安全结束。

Stage 采用 provider 整体 drain 屏障及满足停止/依赖前置条件后的 owner 资源反序清理，详见[一致性规格](runtime-conformance.md)。事件在途调用 pin 和严格 readiness 另列 D-08/D-09。当前 DSH vendor 的顶层 effects 可并发，服务撤销等待放在 provide disposer 内；论文 Algorithm 5 把等待依赖者放在整体恢复之前。Stage 的规则有明确差异记录。[S1][L2]

等待超时不是任务已结束的证据。无法确认 drain 完成时保持该代次拒绝新接入，记录未完成资源和在途命令；不能强行释放连接池后宣布安全重载。无法协作退出的可信代码最终依赖进程终止恢复，持久命令状态由下一次启动对账。

## 5. 领域命令与事务

命令注册是 runtime effect；命令执行产生的是业务事实。一期以固定 `EvaluateQualification` handler 验证版本化能力、当前授权及持久结果，后续 `work` 模块在同一契约下提供任务命令。HTTP、CLI、Agent 和 worker 经过同一查询/命令入口，不分别实现自己的授权分支。能力定义、权限action和运行绑定的区别见[核心对象](core-object-contracts.md)。

CommandKernel 为每个并发命令创建独立 UoW / AsyncSession。组件只发布 factory，不共享单个 AsyncSession。业务事务明确控制 commit / rollback，请求依赖不会自动提交。[S6][L1]

第一期采用部署组织持久 AuthorizationGuard 协调授权/准入撤销、资格事实revision和受控结果提交。该方案主动牺牲企业内写并行度，换取可验证顺序；不是生产规模吞吐承诺。WASM计算在事务外进行，提交时检测事实行与集合变化。锁序、授权时间点和全局禁用规则见[一期事务协议](phase-1.md#transaction-protocol)；任务名额与容量竞争以后按[业务切片](business-slice-plan.md)另验收。

```text
认证、输入读取授权、取得固定 handler/module/provider 绑定
→ 短事务读取最小 FactSnapshot，结束事务
→ 独立runner执行有限WASM计算，宿主验证关联与输出
→ 开启提交事务，取得 guard 与必要对象/revision锁
→ 重载当前授权、准入及事实版本，拒绝陈旧结论
→ 原子写入：评估结果 + 命令终态 + 审计 + outbox
→ COMMIT
→ 返回明确结果，异步投递 outbox
```

授权核验失败、容量不足、版本冲突和依赖不可用有不同结果。提交回执丢失用原命令 ID / 幂等范围对账；不能将 unknown 直接转换成失败并换 key 重放。原子命令结果的恢复读取也必须经过当前认证、租户和结果读权限。

### 5.1 运行资源与持久副作用

| 操作 | owner 与恢复方式 |
|---|---|
| 注册命令、事件监听、定时器、后台任务 | ComponentContext / EffectScope；退出时撤销登记、等待任务结束 |
| 打开连接池、客户端、订阅 | provider；依赖者与在途请求释放后关闭 |
| 保存资格评估；后续创建任务承诺、预留容量、记录验收 | 领域事务；已提交后只能由新的授权业务命令改变 |
| 启动一次性WASM runner、进行有限计算 | provider租约与受控进程；输出是候选，不能当作已提交事实；确认退出后回收 |
| 写 outbox 与审计 | 和业务状态同一事务；不会因插件卸载被删除 |
| 发送通知、创建外部 MR、改变实际 ACL | durable attempt / 回执、接收端幂等、对账和必要补偿；不能承诺内存 disposer 撤回 |

论文的可逆前提不涵盖已经跨越系统边界的任意发送行为，也不验证原子 inverse 本身的正确性。[S1]

## 6. 数据、版本与协作方

PostgreSQL 17 保存 Stage 当前组织协作事实，是唯一长期主库；新增模型、约束、迁移、锁、并发和 UTC / 标识符语义以 PostgreSQL 真库验收，SQLite 只承担适用子集的快速测试。MySQL 退出受支持配置；历史迁移方言分支与评审证据保留，不能据此推断仍有双库门禁。

Redis 8 只提供可重建缓存、非权威短期协调和可丢失唤醒。当前授权与撤销版本、幂等结果、outbox/attempt、业务租约及 fencing 权威留在 PostgreSQL；清空 Redis 不得丢业务事实。缓存键隔离部署环境、组织及必要主体/scope/权限版本，命中仍受当前授权约束。PostgreSQL 事务与 Redis 写入不组成原子提交，通知遗漏由权威版本检查和数据库扫描恢复。每个 Redis 使用方须明确超时、不可用和降级/拒绝策略，见[存储策略](storage-strategy.md)。

| 系统 | 权威内容 | Stage 的使用方式 |
|---|---|---|
| ScoreBook | 批准规则、岗位契约、知识和准确版本 | 保存 release 引用、摘要、批准与实际 activation 回执；后续受控加载结构化规则 |
| Stage | 任职、任务、承诺、容量、授权、业务事件、审计与运行记录 | 所有业务入口调用其受权查询与命令 |
| Maestro | 组织理解、配置与知识提案、协调动作 | 通过被授权 API 提案和执行；生成蓝图不直接修改运行数据库 |
| Virtuoso | 员工工作入口、个人 Agent 与私人工作上下文 | 按需提交任务相关最小上下文；私人会话不自动复制进 Stage |
| GitLab / 专业系统 | 实际仓库 ACL、MR、行业领域事实 | 连接器保存请求、外部引用、核验时间与漂移结果，不把 Stage 期望当作已生效 |

Stage 不承担 MaaS、模型 API Key 托管或模型路由平台。Maestro / Virtuoso 继续保有各自模型能力；Stage 只保存业务授权下必要的运行身份、任务与回执引用。

## 7. 建议代码边界

以下是待实施目录，不表示文件已经存在；优先按职责拆分，不要求每项单独发布一个 Python 包。

```text
app/
  runtime/            # Core：Context、绑定、Fiber、effect、事件、诊断
  bootstrap/          # 固定组合入口、配置校验、宿主启动与关闭
  interfaces/         # HTTP、CLI、worker transport 与认证适配
  execution/          # ExecutionContext、CommandBus、UoW 与执行协议
  identity/           # 认证协议、主体映射、可信身份与任职/委托加载
  release/            # 固定制品清单、批准证据、签名准入和激活记录
  wasm/               # 受控runner协议、ABI、预算、回执与生命周期
  authz/              # 保留现有纯授权判定；追加职责分离等条件
  features/
    qualification/    # 一期固定演示命令；资格候选不自动授予权限
    work/             # 后续工作、承诺、容量、交付、验收
    audit/            # 业务审计/事件记录
    notification/     # outbox 投递与回执、测试 sink
  providers/          # DB、clock、authentication、notification 实现
  models/             # 持久模型
  storage/            # UTC/ExactString、存储适配
  core/               # 现有配置、错误等公共代码；避免继续堆放新领域职责
contracts/            # 只记录已实现且验证过的外部契约
migrations/           # 前向迁移；不由组件 unload 自动执行
tests/
  runtime_conformance/
  commands/
  integration/
```

第一期不要求为了目录美观一次性移动现有文件。先通过 storage provider 包装既有函数，再迁移请求适配和领域命令调用点，保留既有测试。

## 8. 运行与观测

`/health` 保持现有 liveness 含义。新增 `/ready` 的设计要求是根宿主、必需组件、schema 与数据库探测均就绪才允许业务流量；可选通知 sink 不可用不抹去已提交命令，改以待投递和积压状态呈现。readiness 不泄露凭据、租户数据或内部拓扑。

运行诊断记录组件 ID / generation、所需与缺失服务、状态、资源数、在途租约、启动 / 清理失败和关联 request ID。诊断入口受管理权限约束；它与租户业务审计分开存储和控制读者。

第一期固定单 worker。长期每个进程都有自己的 registry 和 observed revision；配置变更必须经过持久 desired revision、逐实例确认与路由门控，才能宣称全局生效。FastAPI 的多个进程不会共享内存 Context。[S8]

## 9. 验收与演进

第一期包含 Core、真实认证、WASM及最小签名准入的联调闭环，详见[一期方案](phase-1.md)。P1a 的 Core 34 项＋S1 12 项已验证；I1/W1/O1/F1 继续按后续阶段验收。任务业务切片、通用 Loader、工作流、多节点、隐私协议与代码替换另行建设，见[路线图](roadmap.md)。

外部来源 `[S1]`～`[S8]` 与本地证据 `[L1]`、`[L2]` 的版本、定位和限制统一见[调研记录](research.md)。
