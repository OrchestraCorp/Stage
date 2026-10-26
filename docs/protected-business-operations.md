# 受保护的人员业务入口

状态：现行实现，2026-10-26。用于可信业务模块，公开 HTTP 合同不变。

## 入口与职责

`app/providers/human.py` 提供 `HumanOperations`。业务组件声明 `IDENTITY` 和
`STORAGE` 两个 Runtime 依赖，在构造时传入自己的 `CommandSpec` 集合和请求时限。
每次调用 `request()` 都完成真实人员认证、构造服务端 `ExecutionContext`，并返回
`HumanExecution(context, kernel)`；Identity/Storage 调用租约和总时限覆盖整个上下文。

`create_human_kernel()` 将同一个 `AuthenticatedAuthorizationLoader` 同时装为事实加载器、
锁协调器和提交前身份验证器。业务模块不再分别拼装三个钩子，也不从请求 body、
`/identity/me` 响应或先前权限结果构造可信身份。一个请求仍只选择一个任职。

工作项的标题/说明编辑、读取和幂等结果恢复已经使用此入口，见
[`HumanWorkService`](../app/providers/work.py)。下一条人员业务可以复用相同入口；
领域 handler、DTO 和资源加载仍由各业务模块定义。

## 写入与恢复

在 `request()` 上下文内调用 `operation.kernel.execute(operation.context, envelope)`。
领域写入继续使用注册的 `CommandSpec`，由内核统一负责当前授权、对象版本、幂等、
业务事务、审计、outbox 意图以及提交前重验。

幂等结果恢复使用 `get_result()` 或 `get_result_by_key()`，依然重新检查当前读取权限。
新登录得到的认证证据不改变原命令的审计记录，也不改变业务幂等语义。

当前短命令和公开原键恢复先取得逻辑幂等键的 PostgreSQL 事务 advisory 锁，再按
TrustState S → Principal S → 组织 S → 工作项写 X／读 S 的顺序执行。同键锁身份为
组织、操作者、命令名称／版本、原幂等键；目标、payload、任职不进入锁键。
原键查询仍是查询，不会在前序回滚后自动执行命令。内部 `get_result(execution_id)`
保留明确的组织 X 等待语义，新增生产消费者前须按[退出条件](stage-concurrency-improvement-plan.md#transition-exits)另行设计。

普通 handler 不得修改自身依赖的身份／授权事实，不得嵌套调用另一 kernel 操作；
内部子任务同样继承此限制。当前协议只适用于单事务短命令，事务锁不是未来
WASM、人工等待或 Worker 的持久执行租约。

## 查询与披露前重验

`CommandKernel.read(context, action=..., target_ref=..., reader=...)` 是公共受保护查询入口。
它接受一个可信的异步数据库读取函数，不定义查询语言或动态查询配置。

执行顺序为：

1. 检查请求时限和认证有效期，建立受管事务。
2. 按 TrustState S → Principal S → 组织 guard S 的顺序取得锁；工作项读取使用 S。
3. 加载当前身份与授权事实，检查指定 action 和目标。
4. 调用业务 `reader(session)`，构造完整的返回值。
5. 在相同事务与锁内再次完整检查当前身份、MFA、任职、关系及时间约束。每个授权阶段只做一次身份复核，末尾检查没有被缓存替代。
6. 回滚只读事务、释放资源，然后交付返回值。

查询应返回已物化的 DTO、不可变值或已复制的数据；不要返回依赖会话的 ORM 实体、
异步生成器或延迟查询结果。读取函数不执行外部 I/O，也不提交、回滚或替换根事务。
`UnitOfWork` 会拒绝误用事务所有权；它不是针对恶意 Python 插件的隔离机制。
业务写入必须使用命令入口，不能依赖查询事务持久化。

`app/storage/coordination.py` 将持锁证明绑定准确根事务与连接事务；错误组织、
跨事务复用、S→X 升级和保存点均被拒绝。即使 handler/reader 捕获保存点异常，
kernel 也会在提交或披露前拒绝失效证明。旧 `lock_tenant()` 已移除，所有组织
保护必须显式指定模式；组织入口先经过同组织事务advisory S/X准入门，再取得guard行S/X，避免后发共享行锁读者持续超越等待写者。控制面授权写者和未默认启用的Outbox继续显式使用X。

## 可信组件的显式装配

`app/providers/execution.py` 的低层 `command_plugin()` 仍允许显式注入 loader、coordinator
和 `identity_validator`，用于可信定制装配与测试。携带人员认证证据却漏掉协调器或
身份验证器时，内核会拒绝执行。公开人员业务优先使用 `HumanOperations`，由它同时持有
身份和存储服务的生命周期租约。

## 回归证据

- `tests/execution/test_protected_reads.py` 覆盖未授权时不读取、读取期间凭据/请求/任职/关系
  到期、事务越权和失败清理。
- `tests/runtime/test_execution_components.py` 验证通用组件保留真实身份钩子，以及撤销后
  原结果不再可读。
- `tests/api/test_work.py` 保留真实认证到工作项编辑、读取、结果恢复和超时释放的合同。
- `tests/native_auth/test_audit_identity.py` 保留原生认证、MFA、任职和关系在最终读取期间
  到期时禁止披露的回归。
