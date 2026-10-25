---
title: Stage P1 WASM 宿主与隔离执行规格
status: design-proposal
implementation_status: not-started
policy_effect: none
specification: stage-wasm-host-v1
research_date: 2026-10-24
---

# Stage P1 WASM 宿主与隔离执行规格

本规格的 tenant/organization 引用均表示服务端唯一的组织授权根；根不等同法人公司，也不由 guest 或请求选择。组织单元、授权 scope 和跨实例身份的边界见[组织授权根语义](organization-root-semantics.md)。

本文件把 WASM 整体基座前置到 P1：P1c 建设独立进程 runner、签名制品准入和受限 ABI，P1d 用 `EvaluateQualification` 验证真实认证、组件生命周期、隔离计算与领域提交之间的完整路径。P1a 的 Core 和 P1b 的真实协议认证是其前提；测试身份 fixture 不能代替认证链验收。

这里是待实现的工程设计。没有新增依赖、运行器、签名服务、业务接口或测试结果；下文 W1 场景当前执行 **0 项**、通过 **0 项**。这套场景与 [Core 的 34 个场景](runtime-conformance.md)分别计数。它不表示通用流程 DSL、带外部副作用的 WASM 插件或完整企业发布治理已完成。

## 1. 自研范围与 P1 执行面

Stage 复用 Wasmtime 的 WebAssembly 验证、编译和执行引擎，自研制品准入、调用协议、进程监督、预算、审计和业务适配。自研范围不包括 WASM 虚拟机、密码算法或身份协议。

| 部分 | 责任与信任边界 |
|---|---|
| Stage 控制进程 | 认证、组织授权、批准记录、制品选择、输入读取、请求幂等、事务提交及 runner 监督 |
| `WasmRunnerProvider` | 由 Component Runtime 管理；提供版本化调用服务，控制排队、子进程、代次租约和关闭报告 |
| runner 子进程 | 验证/编译或装载经平台核验的缓存；创建 Store/实例；执行有界调用；返回结构化计算结果 |
| Wasmtime | 按所选功能配置执行 Core Wasm；提供线性内存边界及 fuel/epoch 等运行控制机制 |
| guest 模块 | 处理宿主给出的受限输入，只产生候选计算结果；输出始终按不可信输入校验 |

P1 接受**标准 Core WebAssembly 二进制模块**，不接受源语言脚本、WAT 文本、Core/Component 自动转换或任意 native 插件。模块必须满足 `stage-wasm-core-profile-v1`：

- imports 为空；不链接 WASI、网络、文件、环境变量、时钟、随机数、日志或业务 hostcall。
- 只提供一个命名为 `memory` 的 32 位非共享线性内存；必须声明最大值，且不超过平台配置上限。
- 只启用平台锁定并验证的 Core Wasm 特性集合；拒绝共享内存、线程、memory64、多内存和 Component 二进制。其他 Wasmtime 可选提案不因引擎默认启用而自动允许。
- 入口是第 3 节定义的有界 bytes ABI。模块不持久保存跨调用状态，不自行加载包，也不决定 tenant、actor、能力上限或提交事务。
- 模块可以包含 Wasm 的 start section；其执行与内存初始化同样计入预算。不能把应用约定的 `_start` 导出和实例化自动执行的 start function 混淆。[S4]

imports 为空是 P1 的明确限制，因此默认没有外部副作用 hostcall。将来增加读写能力时必须发布新的宿主接口规格、授权/预算和副作用协议，不能只修改 linker allowlist 而继续沿用本规格的纯计算保证。

## 2. 官方能力与设计前提

本轮读取的 Wasmtime Python API 提供 Module、Store、Instance、Memory 等基础对象，并暴露 fuel、epoch deadline 和 Store 限制接口。官方文档还明确了几个不能跳过的边界：实例化可能执行模块代码；fuel/epoch 不替宿主中断阻塞调用；Store 内存限制不等于整个进程内存限制；预编译反序列化字节必须来自可信来源。[S1][S2][S3][S4][S5]

实现前必须锁定 `wasmtime-py`、底层 Wasmtime、Python、目标 OS/架构、功能配置和制品格式，记录实际版本及供应链摘要，再用 W1 验证。本文引用当前官方 API 作为可行性证据，未选择或验证任何具体发行组合，也不承诺 Python binding 与 Rust/CLI 的全部功能同步。

WIT 用于 Component Model 的 interfaces/worlds 和类型契约，不定义业务行为。P1 的 bytes ABI 是 Stage 自己的 **Core Wasm** 协议，不能称为已实现 WIT、WASI Preview 2/3 或 Component Model 互操作。后续若采用 WIT，应新建 ABI 版本与兼容适配，独立验证 Python binding、资源句柄和异步 host 函数；不能把旧模块静默按新 ABI 解释。[S6]

## 3. `stage-wasm-bytes-v1`：类型化业务数据的 bytes ABI

### 3.1 函数与内存协议

Core Wasm 函数签名传递的是整数和内存范围；业务类型由固定版本的输入/输出 schema 定义。P1 规定以下导出，签名不匹配即拒绝：

| 导出 | Core Wasm 签名 | 规则 |
|---|---|---|
| `memory` | 32 位、非共享 Memory | 单个线性内存；宿主只通过边界检查后的 API 复制 bytes |
| `stage_abi_version` | `() -> i32` | 必须返回 `1`；调用本身也执行 guest 代码并受预算限制 |
| `stage_alloc` | `(size: i32) -> i32` | 返回本次实例内有效的缓冲区起点；宿主检查长度、范围、容量和重叠 |
| `stage_invoke` | `(input_ptr: i32, input_len: i32, output_ptr: i32, output_capacity: i32) -> i32` | 非负值是实际输出字节数；负值是协议规定的拒绝/失败码，不能当输出长度 |

宿主为每次调用新建 Store 和实例，先完成模块实例化，再核对 ABI 版本，申请输入与输出缓冲区，复制输入并调用。实例化、ABI 查询、分配函数和业务入口共用本次有限预算，不能只计量 `stage_invoke`。

`input_len`、`output_capacity` 和 `stage_alloc` 的 size 必须处于 `0..2^31-1`，同时低于预算上限；非空业务 JSON 的实际输入/输出不得为空。`stage_invoke` 的负码固定为 `-1` 输入不符合模块要求、`-2` 输出容量不足、`-3` 模块声明的计算拒绝、`-4` 模块内部失败，未知负码按 `invalid_output` 处理。负码路径不读取输出区，也不能按模块请求自动提高预算或在同一实例里重试。

所有 pointer/length 均是不可信的整数。宿主按无符号 32 位内存偏移解释 pointer，先检查 `ptr <= memory_size` 和 `len <= memory_size - ptr`，再访问；不先执行可能溢出的 `ptr + len`。输入/输出区间必须互不重叠。`stage_alloc` 或 `stage_invoke` 可能增长内存，宿主每次读写前重新取得当前大小，不缓存跨 guest 调用的原始 native pointer。

输出长度必须非负、在预留 output capacity 内，且对应区间在当前内存内。检查通过后复制到宿主自己的受限缓冲区，再解码；不得将指向 guest 内存的 view 交给后续事务使用。P1 每个实例只调用一次业务入口，复制完成或发生任何失败后销毁实例/Store并结束一次性执行进程，不需要 guest 提供跨调用 allocator/free 生命周期。

### 3.2 数据、错误与输出

输入和输出均为有大小上限的 UTF-8 JSON，按 `input_schema_id/version`、`output_schema_id/version` 精确校验。拒绝重复字段名、非有限数字、超深对象、超长数组/字符串、未知必需版本和不允许的字段；业务数值优先采用有界整数或明确格式的十进制字符串。模块不能通过输出字符串构造 HTML、SQL、文件路径或后续任意命令。

输入规范化编码后的 exact bytes、schema 版本和事实快照引用进入 invocation 摘要。输出验证同时检查业务 schema 与平台资源限制；模块自己的 `ok=true` 不是宿主调用成功、组织授权成立或业务提交成功的依据。明确区分：

| 结果 | 意义 |
|---|---|
| `computed` | 有限计算结束，返回 bytes 和类型验证通过的候选输出；尚未改变业务事实 |
| `guest_rejected` | 模块返回已声明的业务输入拒绝；不自动转成授权拒绝或系统故障 |
| `trap` / `budget_exceeded` / `invalid_output` | 无可采信输出；原始 trap/backtrace 不直接暴露给普通用户 |
| `cancelled` / `runner_failed` | 当前 attempt 已由宿主终结或执行进程异常退出；是否有已提交业务结果仍由 CommandExecution 确认 |
| `stale_inputs` / `authorization_denied` / `artifact_revoked` | Stage 的最终核验未通过；不能保存为有效资格结果或继续业务动作 |
| `commit_unknown` | 数据库提交回执不确定，保留稳定业务幂等身份并对账；不能靠重跑 WASM 解决 |

Core Wasm 没有 I/O imports 可以缩小非确定性来源，但不自动证明任意算法确定、无信息泄露或结果正确。若某能力要求可复现，必须声明其数值、排序、编码和编译特性规则，并用固定输入、不同被支持平台及重复执行测试验证；不将 fuel 计数视为跨引擎版本稳定的计费或正确性证明。

## 4. 签名制品准入：批准授权与验签分别成立

P1 的最小输入是两个不可变对象：`module.wasm` 与严格 schema 的 `WasmReleaseManifest`，加一个独立签名对象。不支持任意压缩包解压或包内相对路径引用，避免准入链顺带引入路径解析与多文件替换风险。

manifest至少包括：`manifest_version/encoding_profile`、`artifact_id/version`、`module_sha256`、模块字节数、`abi_version`、Core feature profile及digest、输入/输出schema版本及digest、`capability_id/version/spec_digest`、目标tenant/environment、预算profile及digest、`governance_policy_ref/digest`、发布序号、前置activation revision、有效期。P1 imports固定为空。所有执行引用按不可变摘要解析，不能只靠可变版本名。准入另记录由平台权威提供的trust_config_revision/key_status_revision；不能把manifest自报值当当前信任状态。

### 4.1 最小批准和签名方案

1. 上传或构建产生候选 module/manifest，并计算 exact bytes 摘要；Stage 展示语义差异、目标租户/环境、能力、预算和前置版本。
2. 真实认证后的主体通过 Stage 发布批准命令提交批准，服务端核验当前有效的发布能力、tenant/scope、所需近期强化认证证据及对象版本。P1 默认一名具备明确批准能力的主体；多方门槛、职责分离与企业治理策略后续扩展，不能虚构已获批准的实际企业规则。
3. Stage 在持久记录中把批准者、认证证据引用、授权决策、精确 manifest/module 摘要、发布序号、有效期及目标环境绑定。认证 token、Passkey 私钥和完整认证断言不随模块交给 runner。
4. 独立的受控发布签名组件只对上述已批准且未撤销的 manifest bytes 签名。P1 建议采用标准 JWS 与平台固定的 `ES256` 算法/密钥类型，使用经过验证的库与密钥托管实现；不自行拼接 ECDSA 签名格式。此算法选择是工程默认方案，仍须安全评审和互操作测试，不能由 manifest 的 `alg` 自选。[S7]
5. admission verifier 先按平台配置允许的 key ID/算法验签，再检查批准记录、当前撤销状态、tenant/environment、有效期、发布序号、所需能力上限和 expected revision。对象存储拿回的 bytes 必须与批准/签名摘要一致，然后才进入隔离验证编译。

构建阶段按冻结的encoding profile规范化一次并保存manifest UTF-8 bytes；ApprovalIntent引用该bytes的digest，JWS payload原样使用该bytes。验签时不经过YAML转换、字段重排或重新序列化。采用标准分离payload表达时按JWS规定还原签名输入；manifest严格解析拒绝重复字段，执行语义必须从同一已验证bytes解析，不能使用另一份“等价内容”。[S7]

`alg`、`kid` 和本协议使用的类型标识必须位于受签名保护的 header；key ID 只能查平台受控信任库。拒绝 `alg=none`、重复或冲突 header、未知 critical 参数和包自带的外部取钥地址；不跟随任意 `jku`、`x5u`，不把包自带公钥自动加入信任库。固定发布序号不能被回退包重置；紧急回退也形成新的受权 activation 记录。

Passkey/2FA 用于证明本次批准操作经过相应认证，不等于直接为模块提供发布签名。签名有效也不等于批准者仍有权限、制品当前未撤销或调用者有权使用该能力。明确记录 `signature_valid`、`approval_authorized`、`admission_allowed` 与 `activation_observed`，不能合并成一个布尔值。签名密钥轮换与信任根修改属于平台受控配置，业务包不能修改自己的信任链。

P1 不要求每次调用都重新进行人类批准，但 dispatch 前和最终业务提交前必须检查制品/activation 的当前状态。撤销先于最终提交生效时，正在返回的计算结果不得继续提交。签名包不能授予原调用者所没有的岗位、租户或业务权限。

## 5. 原始 Wasm 与预编译缓存的两条信任路径

标准 `.wasm` 可以是对宿主不可信的程序，但必须经过受限验证编译。Wasmtime `serialize/deserialize` 产物包含编译后的执行代码，反序列化不是再次执行 Wasm 安全验证；官方明确要求仅反序列化来源可信且未篡改的产物。[S5]

因此 P1 拒绝租户上传 `.cwasm`、Module/Component serialized bytes 或声称由租户编译的 native cache。**租户管理员批准/签名不能把这些 bytes 变成平台可信编译产物。**平台缓存只有在受控编译链生成、完整性与构建来源校验通过后才可被 execution runner 读取；无法证明来源时丢弃并从获准的原始 Wasm 重编译。

缓存键至少包含以下全部字段：

```text
module_sha256
+ Core feature profile / ABI version
+ Wasmtime engine version + wasmtime-py version
+ compiler strategy / engine build identity
+ normalized Engine compile configuration digest
+ target triple / architecture / required CPU features
```

产物元数据还须记录原始 module digest、编译器/构建镜像摘要、目标环境和缓存 bytes digest。任何影响编译语义、安全检测或兼容性的配置变化都进入 key；不只用包名/版本字符串命中缓存。cache hit 仍执行该租户的 admission 和撤销核验，不能沿用另一个租户的授权结果。

P1 可先禁用持久预编译缓存，或实现平台生成缓存及其完整性封装；二者须在验收配置中明确，不存在“来源不明时先 deserialize 试试”的回退。缓存目录对执行 guest 不可写，runner 只收到已经验证的内容句柄或 bytes，不按 guest 给出的路径/URL加载。跨租户缓存命中信息不作为公开接口；共享代码缓存不等于共享 Store、Memory、输入或调用结果。

## 6. 全过程预算与独立进程

### 6.1 预算是准入条件

每个被激活的能力必须绑定一个有限 `BudgetProfile`。至少配置模块/manifest 大小、验证与编译墙钟/CPU/RSS、函数与表复杂度上限、线性内存/表/实例数量、Wasm stack、fuel、调用墙钟、输入/输出 bytes、JSON 深度/元素数、日志 bytes、每租户并发/队列长度和总宿主并发。缺失、不支持、零意义或无限配置不能被解释为“使用引擎无限默认值”，部署不进入 ready。

实际数字是待基准测试和部署容量确认的运行配置，不在本规格中捏造已验证阈值。平台上限与能力申请取较小值；租户批准只能收紧预算，不能扩张平台上限。限额 profile 本身版本化，修改后重新进行准入与兼容检查。

| 阶段 | 必须控制的资源 | 不能依赖的错误假设 |
|---|---|---|
| 接收与解析 | 上传 bytes、manifest/schema 深度、模块扫描复杂度、排队量 | “验证发生在运行前，所以无需资源限制” |
| Wasm 验证与编译 | 独立进程 CPU、RSS、墙钟、输出制品大小、并行编译数 | guest fuel 会给编译器计量 |
| 实例化 | Store limits、初始/最大 memory、table/instance 数量、fuel/epoch、进程总预算 | start function 尚未到业务入口，所以可以无限运行 |
| ABI 查询/分配/调用 | 同一 Store 的剩余 fuel、epoch deadline、总调用 deadline | 每次调用导出都重置预算，或者只给 stage_invoke 计量 |
| 输出复制与解码 | 输出长度、内存边界、结构复杂度、宿主解析 CPU/RSS | guest 输出通过了线性内存检查就一定适合交给业务层 |
| 队列与返回 | per-tenant/global 并发、队列长度、IPC帧与日志大小、截止时间 | 单个模块有限意味着整个平台不会被请求数量耗尽 |

Wasmtime fuel 需要在 Engine 配置开启，并在 Store 中设置有限数额；epoch 需要宿主推进 Engine epoch，Store deadline 本身不是自动计时器。两者在创建 Instance **之前**配置。独立监督者使用单调时钟监控编译和执行进程，作为 guest 不协作、引擎/FFI 故障或 epoch 未推进时的最终停止机制。[S2][S3][S4]

Store 的 memory/table 等限制不包含所有编译器、JIT code、Python对象、宿主临时缓冲和线程栈分配。因此还需 OS 层的进程 CPU/RSS/地址空间、文件描述符、进程数和存活时间约束；不同 OS 的机制必须实际验收，不能把未支持的 limit 静默忽略。[S2][S3]

### 6.2 子进程生命周期与权限

P1 使用独立、一次性执行 worker：每个 invocation 创建新 Store/Instance，执行结束销毁；不能在不同主体、任职或 invocation 间复用带状态实例。编译可以在同一个受限的一次性进程内完成，或在单独受限编译进程中完成后经可信缓存交给执行进程。任何阶段都不在 Stage 的 ASGI event loop 内同步运行 guest/JIT。

启动采用清晰的 spawn/exec 模型，环境变量白名单和显式 FD 继承；不 fork 一个已持有数据库连接、认证 token 或发布私钥的 Stage 状态作为默认 runner。runner 不接收数据库 DSN/连接、IdP access/refresh token、用户 session cookie、签名私钥、云管理凭据或任意 Stage 管理 API 凭据。

P1 guest 无 host imports；runner 还须具备部署级隔离：专用低权限身份、受限文件系统/临时目录、禁止出站网络、禁止派生未受管进程、只保留必要 IPC、CPU/内存/FD限制，以及可终止的整个进程组。平台支持矩阵必须分别记录 Linux/其他目标 OS 的实际隔离机制；本机跑通并不证明部署边界成立。

进程隔离不等于已经获得完整敌对代码沙箱保证：Wasmtime、Python binding、FFI、编译器、OS机制和宿主代码都属于受信任计算基础，需要版本更新及负向测试。WASM 也不对宿主保密；输入一旦提供给模块/runner，就不能再声称模块看不到它。普通日志不记录原始输入、输出、memory dump或完整 trap backtrace。[S1]

未来 hostcall 必须单独限定能力、输入范围、时间、输出和调用次数；fuel/epoch 不能打断阻塞中的宿主函数。尤其不得向新插件提供任意 HTTP、SQL、文件路径、Python对象或反射接口。P1 没有这些 hostcall，不能用尚未实现的未来接口来宣称具有安全外部动作能力。[S3]

## 7. 有边界的 IPC 与取消

Stage 与 runner 使用父进程创建的私有 IPC channel，固定 `protocol_version`、长度前缀/总 bytes 上限和严格 schema；不使用 pickle，不将包内 Python对象反序列化，不把字符串拼进 shell。接收端在分配大缓冲前检查帧长度，禁止 guest 输出伪造多条 control frame。

每次请求在宿主登记：`invocation_id`、稳定 `business_execution_id`、`attempt_id`、随机一次性 nonce、tenant 内部引用、module/manifest digest、ABI/schema/budget版本、provider generation、输入 digest、deadline 和输入 facts revision 引用。runner 仅取得执行所需的最小字段和数据；认证秘密与完整授权投影留在 Stage。

结果 envelope 必须匹配父进程登记的 channel、invocation、attempt、nonce、module/input digest、provider generation 和输出 schema。重复、迟到、乱序、跨进程或旧代次响应不得推动另一个请求的状态；nonce 只解决关联/重放，不把不可信输出变成正确结果证明。tenant/actor 由 Stage 已登记的请求确定，不从 runner echo 中反向建立可信身份。

取消按受控状态转换处理：停止接入/出队，标记当前 attempt 不再可接受结果，要求协作终止；未在有限期限内退出则终止整个 worker 进程组，并等待实际退出与 IPC 关闭。仅发送 cancel 或达到 timeout 不能算资源已释放。取消与响应到达的竞争在宿主串行判定，终态后响应只记诊断，不再提交。

P1 runner 无外部副作用，崩溃后可在预算和重试策略允许时新建 attempt 重算；逻辑调用及业务幂等键保持不变，重新检查 admission、身份和当前输入。计算重复不等于数据库可以重复提交。若 Stage 已进入提交且回执丢失，必须查询原 CommandExecution，状态保持 `commit_unknown` 直到确认；不能报告 `cancelled` 来暗示业务未发生。

## 8. 与 Component Runtime 的资源归属

`WasmRunnerProvider` 的配置、受控编译缓存访问、进程监督器与队列属于 `ComponentContext`；每次调用持有该 provider 的代次租约，runner 进程、IPC、deadline timer和临时文件属于该调用的 EffectScope。`ExecutionContext` 关联一次业务动作，`AuthorizationContext` 继续由服务端构造；三者不能合并。

provider 换代遵循 [Core 的整体 drain 屏障](runtime-conformance.md)：先拒绝旧代次的新租约和队列接入，处理未开始调用的取消/重排，等待正在运行的旧调用结束或被确认终止，再释放旧进程监督与缓存资源。只等待旧代次，不等待新 consumer 就绪；不得持有全局 registry 变更锁等待 worker 退出。

进程组无法确认终止、IPC 仍活动或清理失败时，provider 返回结构化失败并保持相关接入关闭；不能通过删除 PID 记录假装已完成。runner 崩溃只影响其调用和所属 provider 的健康策略，不能直接结束 HTTP 主进程。单次错误、连续崩溃和宿主资源不足分别有退避/熔断策略，重启次数也有限。

租约直到调用进程真实退出、输出已复制或丢弃且请求资源清理完成才释放。Stage 可以在获得受限计算结果后释放 runner 租约，再开启最终业务事务；结果通过固定调用身份、digest和事实版本关联，不需要把 WASM 实例或数据库事务一直保留到提交完成。

## 9. P1d：`EvaluateQualification` 的真实垂直验证

演示使用实际认证协议产生的主体，通过当前组织成员/任职和能力判定调用固定的资格评估能力。输入是 Stage 有权读取的最小事实投影，例如资格标识集合与规则参数；模块返回结构化的匹配结果和允许的原因码。它不能签发任职、授予权限、自动领取任务或把自身输出作为授权事实源。

```text
真实认证 → 按一期锁序短持guard/FactSet锁，一致读取当前授权、受限输入及revision
→ 固定已批准 activation/module/schema/budget 与输入版本
→ 结束读取事务，不持数据库锁运行 WASM
→ 独立 runner 返回候选结果
→ Stage 开启新的提交事务并取得既定授权 guard/对象锁
→ 重读当前认证有效性、主体/任职/委托、制品状态、事实和规则版本
→ 全部匹配才写评估结果、执行结果、审计与 outbox
→ COMMIT 后返回已确认结果；断连则按原幂等身份对账
```

输入快照不能只记录“当时读到的每一行 version”。资格成员、证书、关系等集合的新增/删除可能改变结果，即使原有行版本都没变。读取侧必须固定查询语义、筛选范围、排序/归一化版本，以及会随相关集合变化递增的 scope/collection revision；相关写命令在同一权威协议中更新该 revision。提交侧在授权 guard 和相应锁下重读这些 revision，并核对所需对象/规则版本或重建最小事实 digest。

集合revision的覆盖范围必须包含新增匹配行、删除、资格失效、关系变更和规则改变，不能用只覆盖已返回行的hash伪装防phantom。P1统一采用[QualificationFactSet owner与写入协议](phase-1.md#transaction-protocol)，不能完整追踪则拒绝该规则接入，不使用无revision的完整投影比较作为回退。受时间影响的事实额外核验最早失效时间。初次采样数据与revision必须在同一短锁窗口，计算期间全部释放。

不匹配时返回 `stale_inputs`/冲突，不保存为当前有效资格结果；若自动重算，须在同一逻辑幂等身份下增加 attempt、重新取事实并受重试预算限制。输入/规则与模块版本一致也不免除当前授权和制品撤销核验。已完成结果重读仍检查当前读权限，审计和 outbox 的读者范围不因“只是计算结果”而扩大。

## 10. W1 验收场景

这些是 P1 待编写的行为验收要求，全部 **未执行**；不得把静态文档核对或 Core 34 场景计入本表。真实认证路径另外保留其独立验收，W1-26 必须使用该路径完成集成验证。

| ID | 场景 | 必须观察的结果 | 当前状态 |
|---|---|---|---|
| W1-01 | 标准 Core Wasm、精确 ABI/schema/profile 正向调用 | 输出正确复制、类型验证、Store/进程销毁；记录调用与版本 | 未执行 |
| W1-02 | Component、WAT、共享内存、未知特性、非空 imports | 验证阶段拒绝；未执行 start/业务入口 | 未执行 |
| W1-03 | 导出类型错误、ABI版本错误、错误/超界/重叠 pointer | 无越界宿主读写；明确协议失败并销毁实例 | 未执行 |
| W1-04 | 输出长度越界、重复字段、NaN、超深/超大JSON | 在受限解码阶段拒绝，不进入领域提交 | 未执行 |
| W1-05 | module/manifest任一字节篡改或签名不匹配 | admission拒绝，无验证编译后门 | 未执行 |
| W1-06 | alg none/算法混用、未知/撤销 key、替换 manifest payload | 固定算法/密钥与exact bytes校验拒绝 | 未执行 |
| W1-07 | 签名有效但批准无权、过期、跨tenant/environment或前置revision不符 | 区分验签成功与准入拒绝，不激活 | 未执行 |
| W1-08 | 旧发布序号重放、撤销与dispatch/最终提交竞争 | 不回退未经批准的版本；撤销先发生则不提交结果 | 未执行 |
| W1-09 | 上传serialized/.cwasm，或污染平台缓存 | 不对不可信bytes调用deserialize；缓存来源/摘要不符即弃用 | 未执行 |
| W1-10 | 模块相同但engine/config/CPU目标/ABI变化 | cache key变化或明确不兼容；无静默错误复用 | 未执行 |
| W1-11 | 验证/编译复杂度攻击、超大模块 | 编译进程预算触发；Stage API仍可用；无无限重试 | 未执行 |
| W1-12 | start function无限循环或初始memory超限 | Instance创建前预算已生效；超限终结，无未受控执行窗口 | 未执行 |
| W1-13 | ABI查询/alloc/invoke分别无限循环或memory增长 | 共用有限fuel/deadline；不通过逐次重置逃逸限额 | 未执行 |
| W1-14 | epoch推进失效、guest不协作或FFI/runner异常 | 独立监督者最终停止进程；确认退出后才释放租约 | 未执行 |
| W1-15 | JIT/Python/宿主缓冲耗尽内存或stdout/IPC泛洪 | OS/IPC上限生效；不能只依靠Wasm memory上限 | 未执行 |
| W1-16 | 不同tenant并发、同tenant高负载、队列溢出 | 独立Store/实例/数据；公平限额与明确背压，无租户混读 | 未执行 |
| W1-17 | 检查runner环境、FD、文件、网络与派生进程能力 | 无DB/IdP/session/签名秘密；部署OS隔离实际生效 | 未执行 |
| W1-18 | 伪造nonce/invocation/attempt/digest/generation、重复或迟到响应 | 不推进错误请求；丢弃并记录安全诊断 | 未执行 |
| W1-19 | 取消与结果同时到达、超时后子进程仍活着 | 一次终态判定；确认进程终结；无迟到提交和孤儿 | 未执行 |
| W1-20 | provider换代时存在排队与运行调用 | 停旧接入、处理队列、drain旧租约、清理后换代；不等新代次形成死锁 | 未执行 |
| W1-21 | 子进程崩溃、持续崩溃、清理失败、监督器重启 | 主进程隔离；有限退避/熔断；失败不伪装ready，重算保留逻辑幂等身份 | 未执行 |
| W1-22 | 成功计算后业务事务失败或提交回执丢失 | 计算成功不等于业务成功；无重复提交，unknown按原执行记录对账 | 未执行 |
| W1-23 | 计算期间已有事实行、规则或对象版本改变 | 最终事务拒绝陈旧结果，不能沿用输入时允许决定 | 未执行 |
| W1-24 | 初次读取期间或计算期间集合增删改，原有行version可不变 | 初次数据/revision一致采样；collection revision检出后续变化；无完整writer协议的规则拒绝接入 | 未执行 |
| W1-25 | 模块输出伪造身份、能力、岗位授予或后续命令 | schema和领域边界拒绝；候选资格结果不能扩大权限 | 未执行 |
| W1-26 | 真实认证后执行EvaluateQualification并持久提交 | 受限输入、无锁计算、事务重验、结果+audit+outbox原子记录，全链关联 | 未执行 |
| W1-27 | 同输入重复运行/受支持平台切换与诊断读取 | 按能力声明核验可复现性；敏感payload不进入公共日志，结果读取重验读者权限 | 未执行 |
| W1-28 | 任一强制预算缺失/无效、部署不支持隔离机制 | 启动或activation拒绝，不能以无限默认值或未落实沙箱进入ready | 未执行 |

验收报告记录：Stage commit与未提交差异摘要、宿主/runner/ABI版本、Wasmtime及binding版本、OS/架构/CPU特性、模块/manifest/schema/budget digest、签名key ID与批准记录引用、实际隔离配置、场景ID、命令、故障注入位置、终态、资源清理和持久结果。秘密及原始业务payload不进入公开测试日志；失败、跳过与未运行分别统计。

P1c 完成要求是 W1 的准入、ABI、预算、进程、IPC、生命周期场景通过并保留证据；P1d 再完成真实认证/事实版本/事务的垂直验证。具体阶段门槛由[一期方案](phase-1.md)统一；没有依赖安装、函数壳子或普通 hello-world 可以替代本表。WASM 不自动提供身份认证、保密计算、零知识证明、确定性、业务正确性或分布式 exactly-once。

## 11. 官方来源与核查边界

2026-10-24 核查以下官方文档及其中公开API说明。它们证明可采用的机制与限制，不证明 Stage 的实现已经成立。页面会更新，正式实现应保存所选发行版本的对应文档/源码定位和验收环境。

| ID | 官方来源 | 本文件使用的事实 |
|---|---|---|
| S1 | [Wasmtime Security](https://docs.wasmtime.dev/security.html) | Wasm功能通过imports获得；Wasmtime采用内存/控制流隔离；WASI文件能力由宿主授予；不能推导宿主API天然安全 |
| S2 | [wasmtime-py API](https://bytecodealliance.github.io/wasmtime-py/) | Store的fuel、epoch deadline、memory/table/instance限制；Module/Instance/Memory宿主接口；实际支持以锁定binding验收 |
| S3 | [Wasmtime Config API](https://docs.wasmtime.dev/api/wasmtime/struct.Config.html) | fuel与epoch的执行控制及局限；host阻塞需要宿主治理；引擎配置属于安全与兼容输入 |
| S4 | [Wasmtime Instance API](https://docs.wasmtime.dev/api/wasmtime/struct.Instance.html) | 实例化自动执行模块start function，与导出的`_start`不同；不能晚于实例化才设置预算 |
| S5 | [Pre-Compiling Wasm Programs](https://docs.wasmtime.dev/examples-pre-compiling-wasm.html) | 编译/执行可以分开；deserialize仅能使用受控可信的预编译bytes，不可信产物可破坏宿主安全 |
| S6 | [WIT 概览](https://component-model.bytecodealliance.org/design/wit.html) | WIT定义Component Model接口、world和类型契约，不定义业务行为；与本期Core bytes ABI分开 |
| S7 | [JWS RFC 7515](https://www.rfc-editor.org/rfc/rfc7515.html)、[JWA RFC 7518](https://www.rfc-editor.org/rfc/rfc7518.html) | 使用现成签名封装与算法定义；exact payload、算法限制和可信key解析须由标准库实现 |

Stage 自身边界另见 [AGENT.md](../AGENT.md)、[总体架构](architecture.md)、[Runtime 一致性规格](runtime-conformance.md)和[愿景与自研方向](vision-and-runtime-direction.md)。本文件仅细化 P1 的 WASM 基座，不改变授权事实的权威来源，也不建立 Graphon 依赖或通用业务流程实现。
