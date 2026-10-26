# Stage Runtime 一致性工作规格

规格标识：`stage-runtime-core-v1`

状态：P1a 已实现；Python 行为场景映射 **C 34/34、S 12/12**，真实服务全套 323 项测试通过。证据见[P1a 验收](reviews/p1a-validation-2026-10-25.md)。

核查日期：2026-10-23（Asia/Shanghai）

规格修订：2026-10-25。按[一期框架实施规格](framework-foundation.md)完成 Core、生命周期与应用装配。固定参考源码已实际执行；35 组同描述操作结果一致、9 组预声明差异符合预期。参考片段与完整 Python 场景分别记录，不以片段冒充整体兼容认证。

本文件规定 Stage 如何在 Python 中实现 Cordis 的组件组合思想，以及哪些行为可以对照参考实现验收。它是 Stage 自己的工程工作规格，不是 Cordis 官方标准、认证或跨语言兼容声明。已有 Stage 测试通过，不能计入本规格的一致性通过数量。

2026-10-24 确认[自研组件与流程运行体系](vision-and-runtime-direction.md)。本文件的34个场景只规定组件Core；一期还必须完成[身份](identity-and-authorization.md)、[WASM](wasm-host-design.md)、[共同对象](core-object-contracts.md)和[基座联调](phase-1.md)。不能从Core验收推断其他能力已经完成。Graphon仅用于研究，不作为生产执行依赖。

## 1. 目标、参考和声明范围

目标是让 Stage 的运行资源、服务依赖和插件生命周期拥有可观察、可测试的统一规则。业务处理继续使用 FastAPI、SQLAlchemy 和现有授权能力；插件可组合不改变组织隔离、事务提交和业务授权要求。

本规格使用两类参考，冲突时必须记录差异，不能把两者混为同一标准：

| 参考 | 固定依据 | 用途 |
| --- | --- | --- |
| 论文 | *A Programming Paradigm for Spatiotemporal Composability*，arXiv:2608.25512；本次已读取 PDF 的 SHA-256：`390775dbc9debdcf2ed1b076eed013387ca057630be3cb594617b2b742e48cf0` | 定义 effect/coeffect、组件状态转换、系统边界和恢复前提 |
| DSH 的实际实现 | 本地 Maestro 仓库提交 `6f7124baabca582d43ce84fd59d2f48e4b790763` 中的 `vendor/cordis/`、`vendor/loader/` 和补丁记录 | 对照实际 Context、Fiber、事件和清理行为 |
| 上游来源 | 上述 `vendor/README.md` 记录 Cordis `4.0.0-rc.7`，上游提交 `56b3d4f725681cf4556c1a8695a709cc3b6eed74` | 解释代码来源，不代替包含 DSH 补丁的实际基线 |

正式来源入口为 [arXiv 论文](https://arxiv.org/abs/2608.25512)、[论文 PDF](https://arxiv.org/pdf/2608.25512)、[Cordis 上游仓库](https://github.com/cordiverse/cordis)。本文件不依赖访问兄弟目录才能理解规则；下文的源码路径以表中指定仓库为基准，均作为核查定位记录。

本轮没有确认一个由 Cordis 官方发布的统一跨语言认证流程。对外只能声明经过验证的规格、参考提交、测试范围和偏差。例如：“通过 `stage-runtime-core-v1` 的指定场景；与固定 DSH Core 对照，存在 D-01 至 D-09 的有意差异。”不得简写为“完全兼容 Cordis”或“通过 Cordis 官方认证”。

### 1.1 分层验收

| 层 | 要回答的问题 | 本规格承诺 |
| --- | --- | --- |
| Core | 依赖何时可用、资源归谁、失效时如何清理、异步转换如何收敛 | P1 必须实现和验证，见第 4 节的 8 组场景 |
| Loader | 配置如何生成插件树，变更如何验证、应用和报告 | 后续独立规格；P1 使用显式 Python 组合入口 |
| HMR | 代码如何替换、请求如何停止接入、状态如何迁移、失败如何处置 | 后续独立规格；P1 不承诺代码原地热替换 |
| TypeScript 生态与协议 | TS 插件、模块加载器、配置表达式和工具链能否原样运行 | 不承诺；跨进程集成需要另行定义版本化接口 |

Loader 的声明式配置、HMR 的代码替换、业务工作流持久化是不同能力。实现 Core 不代表后三者自动完成。Python API 可以采用显式方法和异步 context manager；没有必要复制 JavaScript Proxy 的语法外观。

## 2. Stage 保留的能力与新增边界

当前 [应用入口](../app/main.py) 通过 ApplicationRuntime/RuntimeHost 管理组件；[数据库 provider](../app/providers/storage.py) 拥有 engine/session factory，请求 session 持有服务租约直到收尾，事务仍由领域命令负责。[生命周期测试](../tests/runtime/test_lifecycle.py)覆盖启动、故障、恢复、取消和关闭。

运行内核位于 [app/runtime](../app/runtime/README.md)，registry/tasks 的协调逻辑合并在 host.py。公开对象的责任如下：

| 对象 | 职责 |
| --- | --- |
| `RuntimeHost` | 宿主入口，拥有根组件树和 registry，协调启动、就绪、停止接入、drain 与关闭结果 |
| `ComponentContext` | 组件生命周期的服务解析作用域；承载 realm/intercept 继承，以及插件与 effect 的归属 |
| `ServiceLease[T]` / `CallScope` | 不含业务身份的调用保活；固定 owner、binding/generation 和释放状态，由应用执行层持有 |
| `ServiceKey[T]` / `ServiceBinding` | 有类型的服务标识、provider 身份、激活代次、所属 realm 和可用性 |
| `PluginSpec` / `Fiber` | 声明依赖、配置、启动函数及实例状态；一个插件定义可有多个 Fiber |
| `EffectScope` / `EffectHandle` | 建立资源、登记清理、等待清理结束、记录错误与归属 |
| `RuntimeRegistry` | 内部活跃绑定、依赖索引、单个 Fiber 的转换协调；公开只读诊断，不公开可变 registry |
| `EventKey` / `EventBus` | 明确模式和载荷的进程内扩展事件；监听器也是所属 effect |

`ComponentContext`、应用层 `ExecutionContext` 与现有 [AuthorizationContext](../app/authz/context.py) 必须保持三个概念：组件决定依赖哪些能力，单次执行携带业务请求信息并组合 Core 的 CallScope，授权投影提供能力判定所需的可信事实。后两者不属于 Core 公共类型，Core 不导入 execution/authz。`ExecutionContext` 不能由客户端声明替代服务端加载和事务内复核。realm 不能充当租户权限隔离，取得服务对象也不等于取得业务权限。

本规格的 `ctx` API 均指 `ComponentContext`，除非明确标注为请求执行对象。业务请求不得以 `ExecutionContext` 冒充组件 owner 来挂载插件或登记长期服务；请求结束只释放本次执行拥有的 session、租约和其他请求资源。`RuntimeHost` 不保存每个请求的可变身份字段。

持久化边界遵循 [Stage 仓库规则](../AGENT.md)：Agent、连接器、配置蓝图通过受控领域命令执行；写命令包含幂等键、对象版本、审计和失败/未知结果。runtime 卸载不得删除已提交承诺、交付或审计记录，也不得把 effect 的 disposer 当成数据库 rollback、outbox 投递确认或业务补偿。

## 3. 基本术语与状态规则

`provider identity` 指 provider Fiber 的稳定身份。`activation generation` 指该 Fiber 一次加载的代次。每次登记另有不可复用的 `registration_id`，防止同代次注销/重登混淆旧句柄。`binding` 指服务键、realm、provider/代次及登记身份的关联，不是某个业务对象的完整副本。`committed view` 是消费者当前激活代次已接受的依赖绑定集合。`target view` 是按照当前可用服务重新计算、下一次应采用的依赖集合。

状态至少包含 `PENDING`、`LOADING`、`ACTIVE`、`UNLOADING`、`FAILED`、`DISPOSED`。缺少必需服务时等待；加载中建立的服务在整个加载成功后才能对新消费者可用；目标变化不能把旧代次的异步结果直接发布为当前 ACTIVE。UNLOADING 完成前不得开始同一个 Fiber 的下一次加载。DISPOSED 不可重新激活，需要创建新实例。

cleanup 发生错误时继续尝试可安全释放的独立资源，随后进入带 `phase=cleanup` 的 FAILED 并保存聚合报告；仍被活任务/租约使用的依赖记为 blocked，不越过 drain 屏障。被撤销的绑定保持不可用，不自动进入下一次加载。不能只写日志后报告正常关闭、健康 DISPOSED 或新 ACTIVE。未完成的任务、租约和资源必须保留诊断记录；`RuntimeHost` 将失败/未完成结果交给宿主处置，不伪装成资源已经释放。

转换使用每个 Fiber 的独占协调和代次校验。共享 registry 的绑定变更可串行化，但不得在持有全局互斥锁时等待任意插件启动、consumer 清理或业务请求，否则 provider 等 consumer、consumer 等 registry 会形成死锁。通知和执行分开；异步工作始终由明确 owner 跟踪。

`await_ready(required, deadline)` 必须区分“已 ACTIVE”“因缺依赖仍 PENDING”“启动失败”和等待超时，不能将“当前没有运行中的 Task”当作服务已可用。required 由受信任组合入口声明，等待取消不取消共享加载。缺依赖、服务环或父子启动等待环应返回可定位的诊断；不会收敛的组合不得作为健康实例接入请求。固定参考的 `fiber.await()` 可以返回 PENDING，这些严格就绪/环诊断属于 D-09。

### 3.1 两条读取路径

| 路径 | 语义 | 使用要求 |
| --- | --- | --- |
| 声明绑定读取，例如 `ctx.require(key)` | 解析自身已 provide 项、当前 committed view，或沿相同服务 realm 的祖先自提供/已声明依赖绑定；在本代次清理结束前继续读取原绑定 | 组件依赖此路径；应用持有 Core 调用租约；无声明链或不可用时失败，不回退实时 registry |
| 公共 `ctx.get(key)` | 观察当前 registry 中该 realm 的 ACTIVE provider；不执行其 availability predicate；缺失返回 absent | 用于发现与诊断，不取得调用租约，不改变 inject；对象存在不表示可以接新工作 |
| 非 strict 的内部观察 | 可以检查 LOADING/UNLOADING 的注册信息 | 仅供受控运行时诊断；不得作为可调用服务提供给领域代码 |

这是对参考实现两个不同机制的保留：`reflect.ts:233` 的 `get()` 绕过 inject，默认只返回 ACTIVE provider；`reflect.ts:153` 的代理属性读取检查声明并沿 Fiber 保存的绑定解析。不能写成“Cordis 所有读取都禁止未声明服务”，也不能以 `get()` 的成功作为长期持有依赖的依据。

committed view 固定的是依赖绑定关系，不会深拷贝数据库连接、缓存或业务数据，也不是事务快照。参考实现 `reflect.ts:254` 允许当前 provider 修改自己的 `impl.value`，其 Fiber UID 不变且不会自动 notify 或重启消费者。Stage 的 owner/generation 受检 `update_value()` 保留此可见性：重新 require 见新值，已取得的对象引用不改变。该接口限明确允许更新的值型服务；真正替换资源实现必须建立新激活代次并触发消费者重新绑定，不得用值更新绕过换代流程。派生 context 不产生新 Fiber，详见[实施规格](framework-foundation.md)。

## 4. P1 的 8 组必验语义

下列场景是稳定的测试义务，已通过本轮 Python 映射验收；每项对应 nodeid 和结果见[机器证据](reviews/p1a-runtime-conformance-2026-10-25.json)。新的失败路径可追加 ID，不得删除原测试来掩盖不一致。固定 TS 的共享片段与差异另有原始 trace。

### C1：Effect ownership、建立失败与异步清理

每个登记到框架的资源必须归属一个 Fiber/EffectScope。注册监听器、服务、后台任务、子 Fiber、连接池均须产生可追踪的清理句柄。effect 的 owner 记录必须先于用户 setup 执行建立，保证 setup 内重入卸载时资源仍有人负责。

P1 支持同步 setup 返回 disposer、异步 setup 返回 disposer，以及逐步产生 disposer 的同步/异步生成器；也可提供 async context manager 适配器。无资源的 setup 可以没有 disposer。框架不能验证任意 disposer 确实撤销了外部行为，插件作者仍负责原子资源的正确释放。

| 场景 | 操作与验收 |
| --- | --- |
| C1-01 | setup 获取 A、B 后失败：已登记资源各清理一次，保留启动错误，资源清单无遗漏 |
| C1-02 | setup await 时调用父卸载：等待当前步骤收敛并回收该步骤登记的资源，不把旧加载发布为 ACTIVE |
| C1-03 | 手工关闭 effect 后关闭 Fiber；外部并发多次关闭同一 effect：不重复运行逆操作，等待同一清理结果；内部自等待按 S1-07 拒绝 |
| C1-04 | 一个 cleanup 抛错：独立且安全的其余 cleanup 继续；错误聚合，被未收敛任务依赖的资源记 blocked，不能报告正常关闭 |
| C1-05 | UNLOADING/DISPOSED 时注册新 effect、listener 或 child：显式拒绝；归属清单中没有逃逸项 |

参考：`vendor/cordis/src/fiber.ts:418–560`、`:265–319`。重复关闭的等待结果和错误报告采用第 5 节 D-03/D-04 的 Stage 规则。

### C2：Reactive coeffects 与服务可用性

依赖必须由 runtime 自动跟踪，业务调用方不负责反复调用 refresh。服务只有在 provider ACTIVE 且 availability predicate 满足时才可作为新激活依赖；predicate 为纯本地同步快照判断，抛错按不可用处理并报告，网络探测由 provider 的受管任务另行执行。同一 `(realm, service key)` 的登记槽不能重复占用，LOADING 预留和 check=false 均不允许另一 provider 覆盖。公开登记撤销后才可复用槽，旧 committed pins 独立保留。

| 场景 | 操作与验收 |
| --- | --- |
| C2-01 | consumer 先注册、provider 后注册：consumer 先 PENDING，provider 可用后自动加载 |
| C2-02 | 必需服务消失再恢复：consumer 自动卸载再加载，无需外部手动刷新 |
| C2-03 | provider 仍在但 check 结果改变：显式可用性通知后只影响该服务及 realm 的消费者 |
| C2-04 | 重复 provider 和必需依赖环：重复绑定拒绝；环返回组成成员与服务键，健康检查不把等待中的组合报告为已就绪 |

参考：`reflect.ts:277–335`、`fiber.ts:597–623`。C2-04 的重复登记拒绝属于共享语义，环诊断属于 Stage D-09，差分结果分别记录。P1 不提供动态未声明依赖或“忽略不存在的必需服务”模式。启动服务按整代可见，ACTIVE 后单项登记不具有批次事务，见 S1-02。

### C3：Provider 代次与过期异步结果

target 必须反映必需绑定的 provider 身份、激活代次及 registration_id，不是一个“所有键都存在”的布尔量。相同名称、相同配置甚至相同值的 provider 换代也必须触发重新绑定；与当前依赖无关的服务变化不得重启该消费者。

本实例的 change_revision 随目标、配置和关闭意图单调增加，不能只比较依赖 tuple；启动尝试另有 generation。S1-10 追加 A→B→A 和 LOADING 时修改自身配置，检查旧异步完成不会复活。

| 场景 | 操作与验收 |
| --- | --- |
| C3-01 | P1 提供 db，替换为 P2，db 值相等：consumer 清理旧代次后再运行新代次 |
| C3-02 | consumer 加载 await 中依赖从 P1 换 P2 再换 P3：旧结果不可发布，最终只接受有效目标；每次获得资源均被回收 |
| C3-03 | 同一 provider 配置重启：记录新激活代次；旧代次完成通知不能清除新代次的状态 |
| C3-04 | 不相关 provider 增删：消费者执行次数和 committed view 不变 |

参考：`fiber.ts:611–673`。DSH epoch 直接拼接 provider Fiber UID，Stage 显式记录 activation generation 的行为属于 D-05；差分证据不得略去同一 Fiber 重启时的路径差异。

### C4：Committed view 与声明读取

激活前提交依赖绑定视图，激活和卸载使用同一视图。runtime registry 更新只能影响下一目标，不能把进行中的 consumer 清理改绑到另一个 provider。所有旧绑定在该消费者本代次清理完成后释放。

| 场景 | 操作与验收 |
| --- | --- |
| C4-01 | P1 撤销后 consumer 清理中读取 db：仍得到 P1 的绑定；公共 `get(db)` 按实时可用性返回 absent 或新的可用 P2 |
| C4-02 | P1 公开登记撤销后，不同 Fiber 的 P2 独立登记并可用：旧 consumer cleanup 仍用 P1，成功结束旧代次后才绑定 P2；这不是默认 replace 的顺序 |
| C4-03 | 未声明读取与 pending 读取：分别得到未声明/不可用错误；公共 get 不会使声明检查自动通过 |
| C4-04 | 清理完成：旧 committed view 释放，后续读取不得继续使用已经撤销的作用域 |

参考：`reflect.ts:144–166`、`:226–242`、`fiber.ts:647`、`:687`。测试只断言服务绑定身份稳定，不断言服务内部可变数据冻结。

### C5：Provider 整体 drain 屏障与请求租约

Stage 采用比当前 vendor 更明确的资源释放规则：**先停止依赖旧 provider 的新工作接入，通知消费者/child/任务停止生产，再让旧调用和依赖者收敛，最后销毁 provider 资源**。`RuntimeHost` 协调停止接入；请求或事件通过无业务身份的 `CallScope/ServiceLease` 固定 provider/代次，应用 ExecutionContext 组合这些租约；进入 drain 后不再发新业务调用租约。

consumer 的旧代次清理和旧请求租约全部释放后，provider 才能开始关闭连接池、消息连接等资源。等待的是旧绑定的释放，不能因消费者已在新代次运行而继续无限等待。不存在“卸载 provider 就默认业务事务已回滚”的规则。

组件 dependency pin 与短期调用 lease 分别记录：旧 consumer/worker 的 cleanup 保持旧 committed binding，以完成事务回滚和取消记账，不需申请已禁止的新业务租约。关闭顺序、重入等待与失败屏障以[实施规格第 5 节](framework-foundation.md#5-关闭协议与失败结果)为准。stop 回调不能排在它所唤醒的 drain 之后。

失效时在任何 await 前关闭受影响依赖闭包的调用 admission；CallScope 原子检查 binding/registration 及必要依赖可接入状态，再增加 pin。不能在 provider 已退役而 consumer 尚未收到异步通知的窗口继续接受新请求。已选 dispatch 内的全部 listener pins 也参与已知关闭等待环检查。

| 场景 | 操作与验收 |
| --- | --- |
| C5-01 | consumer 在 cleanup await 时仍使用旧 db：engine.close 必须在 cleanup 完成之后发生 |
| C5-02 | provider 有服务撤销、timer、连接池等多个 effect：任何 provider 资源 cleanup 都不能越过整体 drain 屏障 |
| C5-03 | 正在执行请求/事务时要求重配：停止该代次新接入，原请求完成或按明确策略取消并收敛，释放租约后再关闭资源 |
| C5-04 | consumer 卡住、取消或清理失败：不得声称已安全换代；超时保留可定位的失败/未完成记录，不默认强关资源并恢复接入 |

这是相对当前 DSH vendor 的有意偏差 D-01：整体先 drain 的位置与论文 Algorithm 5 第 25 行一致，请求租约则是 Stage 的业务扩展。截止时间和取消策略属于可验证配置；P1 默认失败时保持拒绝新接入，后续重试/进程重启由明确操作触发。恢复过程必须保留业务结果“不确定”的可能性。

### C6：按服务键的 realm 与 intercept

`ComponentContext` 是带继承关系的解析作用域，服务 store 按 `(realm, key)` 区分。`isolate(db)` 只改变 db 的 realm，仍能继承 logger 等未隔离服务。同一显式 realm label 可供兄弟组件作用域共享，不采用“每个 child 的全部服务都私有”的替代规则。

isolate/intercept 视图继承同一个 owner/generation，不产生新 Fiber；只有 plugin 挂载产生结构 child。require 可继承同 realm 祖先的 committed 声明，自身 provide 可读取，不能将所有读取简单限制为当前 Fiber 的 inject 字典。

intercept 是传递给服务消费/解析过程的配置层，不是偷偷替换业务授权结果。合并顺序为祖先到后代；显式 base 最低、head 最高；服务可声明自己的 merge 规则。未声明规则时使用浅层覆盖，不推断深合并。

| 场景 | 操作与验收 |
| --- | --- |
| C6-01 | 子作用域隔离 db：db 可替换，logger 继续共用；父作用域不变 |
| C6-02 | 同标签兄弟共享 db，不同标签独立；只通知受影响 realm 内消费者 |
| C6-03 | 多层 intercept 与消费方 inject 配置同时存在：精确核对先后覆盖结果及服务定义的 merge 调用 |
| C6-04 | 创建和卸载隔离作用域：无遗留 binding/consumer/listener，未授权业务访问仍被 authz 拒绝 |

参考：`context.ts:99–144`、`service.ts:86–101`、`vendor/loader/src/config/isolate.ts`。P1 覆盖程序化创建/卸载 realm；运行中的声明式树迁移留给 Loader 规格。

### C7：调用方资源归属与嵌套插件

DSH 使用 traceable Proxy 将服务内产生的 effect 归属到调用方 Context。Python P1 使用明确的 `owner_ctx: ComponentContext` 参数或绑定到调用组件的 service wrapper；不得把调用方写入共享 service 实例的可变 `self.ctx`。并行调用不能相互覆盖 owner。`ExecutionContext` 中的业务身份不决定组件资源 owner。

每个 child Fiber 同时具有唯一结构父 owner。父卸载必须等待 child 的异步清理结束，清理已被其他调用方开始时也要能加入等待。提供能力的 service 生命周期与调用方贡献的 listener/timer/tool 生命周期分别记录。

| 场景 | 操作与验收 |
| --- | --- |
| C7-01 | A、B 共用 timer，分别登记任务；卸载 A 仅移除 A 的任务，B 和 timer provider 继续存在 |
| C7-02 | A、B 并发经过同一服务登记资源：每条 effect 的 owner 正确，异常和 await 不改变归属 |
| C7-03 | parent 创建 child，child 再登记事件和后台任务；卸载 parent 后全部收敛，registry 无孤儿 |
| C7-04 | 使用过期 owner 句柄调用服务注册：拒绝登记，不能借 provider 的仍存活状态延长已卸载调用方寿命 |

参考：`utils.ts:116–211`、`fiber.ts:265–319`。显式 caller API 是 D-06：保证资源归属结果，不宣称 Python 已复制所有 JS getter、callable service、association 和 shadow Proxy 行为。

### C8：Typed events 与生命周期结果

每个事件用稳定 EventKey 声明载荷、返回类型和唯一 dispatch mode。进程内事件不是 durable domain event；要求持久化的事实由领域事务写入审计/outbox。事件监听注册归属 effect，卸载后不得参与新的 dispatch。

公共事件 dispatch 只选择 ACTIVE 且 admission 未关闭的 owner，入口原子选定快照并取得 owner/generation/registration 调用 pin，包括 serial 尚未调用的监听。卸载撤销新可见性后等待已有 dispatch 收敛；短路释放未用 pin，取消必须等已启动回调实际结束。事件回调不能同步等待关闭当前 dispatch 保活的 owner，使用 request_close。激活门控和调用保护是 D-08，固定 vendor 的上下文过滤/快照/注销本身不提供它。

| 模式 | P1 语义 |
| --- | --- |
| `emit` | 同步按登记顺序调用，不承担异步任务；注册异步 listener 明确拒绝，改用受管理的异步模式 |
| `parallel` | 等待本次选中的全部 listener 完成后聚合错误；一个失败不使其他 listener 变成无 owner 的后台任务 |
| `serial` | 依次 await；遇到显式 bail 结果终止 |
| `bail` | 同步依次调用，遇到显式 bail 结果终止 |
| `waterfall` | around middleware；必须调用 next 才委托下游；可在自身处理前后包装结果，跳过 next 即短路 |

DSH 的 bail 判断把 `null`、`undefined`、`false` 作为继续，其余值包括 `0` 和空字符串都作为终止。Python 不得直接用 `if result` 模拟。P1 使用 `Continue` / `Stop(value)` 明确表示，由 TS 对照适配器转换为相同决策。同步/异步 waterfall 通过不同类型声明约束，但保留相同委托和短路语义。

| 场景 | 操作与验收 |
| --- | --- |
| C8-01 | listener 所属 Fiber 卸载后再 dispatch：不再调用；一次 dispatch 使用入口选定的 listener 集合 |
| C8-02 | waterfall 的前后包装、拒绝调用 next：核对执行顺序和最终值，不能实现成普通值累加 pipeline |
| C8-03 | parallel 中一个失败、一个 await：等待后者完成并汇总错误；serial/bail 不执行短路后的 listener |
| C8-04 | `None`、`False`、`0`、空字符串等映射：按显式 Continue/Stop 结果核对，避免 Python truthiness 漂移 |
| C8-05 | 异步启动/关闭失败、调用者取消、重复 close：结构化记录结果；required dependency 未就绪不能被报告为健康 ACTIVE |

参考：`events.ts:13`、`:183–242`、`:254–301`。parallel 的 allSettled/聚合错误是共享语义；cleanup 失败报告属于 D-04，emit 限制和 Continue/Stop 属于 D-07，在途 listener 保护属于 D-08。

## 5. 与实际 vendor 的有意差异

DSH 当前 `fiber.ts:431–439` 在**一个 effect 内**反序串行执行 disposer；`fiber.ts:675–686` 对 **Fiber 顶层 effects** 使用 `Promise.all` 并发执行，并记录各项异常。这两层不能合并写成“全部严格 LIFO”。

`reflect.ts:297–303` 的 provide disposer 会撤销公开 binding，通知并等待消费者 settled，再删除 provider 自身的服务视图。但等待位于这一个 disposer 内，不能阻止 provider 的其他顶层 effect 同时关闭资源。因此“consumer 还能读旧 service 引用”不等于“它底层的连接池仍然打开”。

论文 §5.1.3、PDF 第 63 页明确说明 `fiber.dispose` 会并发启动一个 Fiber 的 effects。不能从第 62 页的 LIFO 概述推导出“论文要求所有顶层 effect 全局串行”。需要区分单个组合 effect 内逆操作的次序，以及 Fiber 顶层各 effect 的执行并发。

论文与当前 vendor 在此处最关键的差异是**等待的位置**：Algorithm 5 第 25 行先等待所通知的 dependents，之后第 26 行才执行整个 `fiber.dispose()`；第 63 页特意解释等待不能只放在一个 inverse 内，否则其他 effects 仍无顺序约束。Stage 的 D-01 采用这一整体屏障，再加入业务请求租约。D-02 的单 owner 顶层默认 LIFO 则是 Stage 自身选择的更强顺序约束，既不归因于论文，也不归因于当前 vendor。

Stage 以业务执行安全为优先，选择如下有意差异；现已按 P1a 对应证据实现和验证。它们不表示修复或修改了参考实现，也不是官方兼容认证；后文逐项保留其语义范围。

| ID | Stage 工作规则 | 相对参考实现的差异及原因 |
| --- | --- | --- |
| D-01 | provider 整体恢复前等待旧依赖者/请求租约释放 | 等待位置采用论文 Algorithm 5 第 25 行，区别于当前 vendor 的单个 provide disposer 内等待；请求租约是 Stage 扩展 |
| D-02 | stop/join 前置条件满足后，同 owner 资源默认反序串行；独立分支并发须明确声明 | Stage 自身约束，不能仅按树 LIFO 等待依赖；论文与 vendor 允许顶层 effects 并发，不宣称论文要求全局 LIFO |
| D-03 | 外部 close 等待者加入同一操作；取消不终止共享关闭；内部自等待拒绝，另设 request_close | vendor 公开 disposer 为 single-shot，内部组合可加入已开始清理；Stage 额外定义 Python 取消与等待环语义 |
| D-04 | 安全独立清理继续，未收敛依赖记 blocked；错误聚合、拒绝自动重载 | vendor 常以 logger containment 处理 teardown 错误；Stage 不把清理失败或被阻塞资源报告为已释放，单 effect 内失败也不能隐去其他安全清理 |
| D-05 | 显式 activation generation＋change_revision＋每次登记的 registration_id | vendor epoch 使用依赖 provider Fiber UID；Stage 追加配置/ABA 及同代次重登记防护 |
| D-06 | caller ownership 通过显式 owner 或 ScopedService 传递 | 不提供 JS Proxy 表面语法兼容；只对资源归属、隔离和清理结果进行一致性检查 |
| D-07 | emit 仅接受同步 listener；bail 使用 Continue/Stop 类型 | 不复制未等待 Promise 的调用方式和 JS 值类型；避免 Python coroutine 丢失及 truthiness 差异 |
| D-08 | 公共 listener 按 owner 激活/接入门控；snapshot 持有调用 pin，卸载/取消等待在途回调 | vendor 仅上下文过滤、回调快照和列表注销，没有相应激活门控/调用 drain 保证 |
| D-09 | 显式 required/deadline 的 readiness 和依赖/启动等待环诊断 | vendor fiber.await 可在 PENDING 返回；Stage 不能据此报告业务就绪，诊断仅覆盖受管理等待 |

每个对照测试必须标注 `shared-semantics` 或关联具体 D-ID。Stage 的更强约束通过，不能把参考侧的不同输出改写为通过，也不能把所有未理解差异归入“Python 实现方式不同”。其他差异必须新增设计决策后才能纳入规格。

## 6. Loader、HMR 与 TypeScript 生态的后续边界

P1 的组合入口是仓内受控 Python 代码，声明插件和配置后显式启动。后续 Loader 至少需要配置校验、完整 diff/preflight、原始配置和生效配置区分、变更序号、失败结果及持久化加载记录。动态配置不是业务授权，不允许蓝图绕过领域命令直接访问数据库。

DSH Loader 存在 eager、非事务的 entry 更新；其 `vendor/README.md` 明确记录 activation failure 可能留下部分已应用树。Stage 后续应定义自己的失败模型，不能因有 disposer 就宣称任意配置替换原子完成。`!!js` 表达式也不是 Python 的默认支持面。

HMR 后续需另行定义代码代次、请求 drain、状态迁移、导入缓存、失败恢复及多 worker 生效协调。`importlib.reload()` 单独不能提供这些语义。P1 开发代码修改采用进程重启，运行时测试只通过受控 mount/unmount/replace 操作验证生命周期。

TS 插件依赖 Node、ESM/CJS、Cordis declaration merging 和平台能力，不能因 Python 内核采用相同概念而直接加载。若 Maestro/Virtuoso 调用 Stage，集成点是受控 HTTP/RPC/事件协议；跨进程身份、取消、重试、幂等、结果未知及授权均需单独定义。

## 7. 对照测试方法与首次验收

### 7.1 共享场景执行

使用同一组可序列化场景描述操作，不把 TS 或 Python 的实现分支当测试输入。操作包括 mount、provide、撤销、replace、get/require、登记 effect、释放 await 屏障、dispatch、关闭 owner。TS 适配器运行固定 vendor，Python 适配器运行 Stage 候选内核；两端输出统一事件记录。

异步顺序用显式 Event/barrier 和人工推进控制，不能用固定 sleep 推断完成。将“consumer.cleanup.end 必须先于 provider.pool.close”等必要先后关系作为断言；允许并发的部分比较偏序、归属和终态，不要求某一次日志总顺序在两端相同。生成 UID、时间戳可规范化，provider 身份变化、错误类型、状态、资源数量和 owner 不能被规范化抹掉。

首次执行全部 C1–C8 场景，并增加固定种子的操作序列覆盖嵌套依赖、provider 抖动、初始化失败和卸载重入。对照测试之外，单独验证 Stage 的权限、关系、属性、对象版本、事务、幂等和审计边界；一致性测试不代替这些业务测试。

### 7.2 P1a / Core 最小接入切片

1. 实现 Core 与 8 组场景，不先扩展 Loader/HMR。
2. 数据库 provider 拥有 engine；会话工厂作为受声明依赖提供，清理受 D-01 屏障保护。
3. 一个测试 consumer 通过无业务身份的 CallScope 取得服务代次租约，应用层组合 ExecutionContext 并使用事务；覆盖正常完成、异常 rollback、请求取消和 provider drain。
4. FastAPI lifespan 通过 `RuntimeHost` 等待必需插件就绪后才报告 ready；关闭等待根组件树的结构化清理结果。
5. 授权判定继续由 `app.authz` 提供；将其作为应用服务注入不能改变可信事实来源。已有内部 CommandKernel、审计/outbox 与投递实现继续复用，真实身份与 HTTP 接入分别验收。

P1a / Core 完成条件是 C1–C8 的 34 个场景与[补充 S1 的 12 个边界场景](framework-foundation.md#8-分段交付与补充验收)全部通过、共享语义与 TS 对照一致、D-01 至 D-09 有对应证据、首个真实数据库/请求切片及既有测试通过，且无未解释差异。Stage-only 诊断不要求参考侧产生相同结果。整体一期还须完成[一期方案](phase-1.md)的P1b真实认证、P1c受控WASM/准入和P1d资格评估联调；原任务业务九类验收移到[后续业务切片](business-slice-plan.md)。性能、长期压力及多节点生产运行另验收，不能从Core完成推断整个平台或生产就绪。

### 7.3 当前证据状态

| 项目 | 状态 |
| --- | --- |
| 参考论文与本地 vendor 源码核查 | 已读取；用于制定本工作规格 |
| `stage-runtime-core-v1` 内核 | P1a 已实现并验证 |
| TS/Python 共享场景适配器 | 35/35 相同操作描述比较成功；9/9 预声明差异匹配，见[覆盖记录](reviews/runtime-reference-2026-10-25/coverage.json) |
| C1–C8 已执行 / 已通过 | 34 / 34（Python 义务映射，非官方认证） |
| S1-01～S1-12 已执行 / 已通过 | 12 / 12 |
| 数据库 provider 与请求租约接入 | 真 PG/Redis、命令/worker 装配、取消收尾及设备故障恢复通过 |
| Loader / HMR 验收 | 未开始，不属于 P1 完成声明 |

## 8. 验证记录格式

每轮保留机器可读结果及简短人工报告，推荐在未来测试产物中按 run_id 保存 `manifest.json`、`trace.ts.jsonl`、`trace.python.jsonl`、`results.json`、`report.md`。这里的文件名是产物约定，当前没有生成这些证据文件。

manifest 至少记录以下字段；下文仍是未来运行的格式示例，实际结果以[P1a 证据](reviews/p1a-validation-2026-10-25.md)为准：

```json
{
  "spec_id": "stage-runtime-core-v1",
  "run_id": "<UTC timestamp plus unique id>",
  "stage_commit": "<full commit>",
  "stage_dirty": true,
  "stage_patch_sha256": "<digest or null when clean>",
  "reference_repository": "Maestro",
  "reference_commit": "6f7124baabca582d43ce84fd59d2f48e4b790763",
  "reference_repository_dirty": "<actual whole repository status>",
  "reference_scope_dirty": false,
  "reference_scope_paths": ["vendor/cordis", "vendor/loader", "vendor/README.md"],
  "reference_manifest_sha256": "<hash of file-digest manifest>",
  "paper_pdf_sha256": "390775dbc9debdcf2ed1b076eed013387ca057630be3cb594617b2b742e48cf0",
  "python_version": "<version>",
  "node_version": "<version>",
  "platform": "<OS and architecture>",
  "commands": ["<actual commands>"],
  "seed": "<integer or null>",
  "executed": 0,
  "passed": 0,
  "failed": 0,
  "not_run": ["<scenario ids>"],
  "expected_deviations": ["<D-ids actually exercised>"]
}
```

单个 trace 事件至少包含序号、scenario_id、operation、Fiber/owner 标识、activation generation、change_revision、realm/key、registration/effect 标识、状态和结构化结果。清理开始/完成必须分开记录；错误不能仅存在不可解析的日志字符串中。场景结果保存 TS 结果、Stage 结果、断言、关联 D-ID 和原始证据路径；`not_run`、阻塞、异常中断不得计入通过。

`reference_repository_dirty` 记录整个参考仓库，`reference_scope_dirty` 仅记录本次实际执行到的参考范围；后者为 false 不能推导整个 Maestro 工作区干净。正式执行须将实际触及的基础库和测试适配器加入 scope，并记录其 hash。

参考文件 manifest 应至少包含 `vendor/README.md`、`vendor/cordis/src/` 全部文件、对照适配器及实际执行到的基础库/Loader 文件。源码核查时几个关键文件的 SHA-256 为：

| Maestro 相对路径 | SHA-256 |
| --- | --- |
| `vendor/cordis/src/fiber.ts` | `b7661d6c19be76cef6b97d88719a8112c0f8b5abe65bd9e7d5947773fd4b96d4` |
| `vendor/cordis/src/reflect.ts` | `6847b9781044023d65aebb5896a873b9f0a3d777eaf7fa0dd2a7a408eb6b74a6` |
| `vendor/cordis/src/context.ts` | `96b388162d6013c1898de61e35f28917abb93809de24174980f2a1a348d0b115` |
| `vendor/cordis/src/utils.ts` | `8fe273424583d21267ef13f248840e97454d09d836a0ac08323a9482c2069263` |
| `vendor/cordis/src/events.ts` | `9aff93dcfcad98aebb692c19c36d3bf034a96f9085bc66900a997cda3f79e6fe` |

这些 hash 固定的是本次阅读对象，不是测试成功的证据。更新参考代码、改变公开生命周期规则或增加偏差时，必须同时更新规格修订记录、场景和差分报告；不得只更新一个版本号并沿用旧通过结论。

## 9. 核查定位

| 主题 | 来源 |
| --- | --- |
| effect 组合与恢复前提 | 论文 §3.1、§5.1.1；任意原子逆操作仍由组件作者负责 |
| coeffect、committed/target view、异步生命周期 | 论文 §3.2、§4.4、§5.1.2–5.1.4 |
| 整体 drain 与顶层 effect 并发 | 论文 PDF 第 63 页，Algorithm 5 第 25–26 行及其解释；先等待 dependents，再调用并发启动 effects 的 `fiber.dispose` |
| 资源获得与对外发送、补偿边界 | 论文 §6.1；业务补偿不能自动继承完整逆运算的保证 |
| 与既有动态服务机制的关系 | 论文 §7.4；动态服务先例不等于 Cordis 的完整 effect/context 语义 |
| DSH 当前框架组成 | Maestro `docs/architecture.md`、`docs/cordis-primer.md` |
| 版本和 DSH 补丁 | Maestro `vendor/README.md`，尤其生命周期加固与 Loader 非事务更新记录 |
| Stage 应用与事务边界 | [app/main.py](../app/main.py)、[app/core/db.py](../app/core/db.py)、[AGENT.md](../AGENT.md) |

当前可描述为“Stage 已实现并验证自己的 P1a 组件 Core，按固定 Cordis 源码核对共享语义并记录差异”；不宣称官方 Cordis 认证、完整 TypeScript 生态兼容或生产验收完成。
