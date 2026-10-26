---
title: Stage 授权与业务并发改进提案
status: implemented-validation-record-linked
date: 2026-10-26
policy_effect: current-implemented-protocol-with-explicit-future-gates
---

# Stage 授权与业务并发改进提案

> 2026-10-26 后续状态：用户已批准实施，A–E 的当前代码与复测见[实现复查](reviews/concurrency-optimization-2026-10-26.md)。下文保留方案形成时的现状、静态计数和候选描述；实际接口与实测以该记录和源码为准。第10～12节仍约束后续路线。

本提案结合 [PostgreSQL／授权系统研究](reviews/organization-lock-industry-research-2026-10-26.md)、[OA 实际源码研究](reviews/oa-identity-permission-concurrency-research-2026-10-26.md)和 Stage 当前源码。对照提交 `0560abef3872ad40d406ad4e8a54b3ad35f800e5`；本轮只形成代码映射和设计，未修改应用、测试、数据库、依赖或运行服务。用户随后要求兼顾既定开发／技术路线，新增第10～12节的跨阶段约束、过渡项退出条件和交付门槛。

## 1. 建议收敛的范围

保留当前模块化单体、HumanOperations 公共入口、CommandKernel 单一提交者、PostgreSQL 权威，以及撤权和业务提交的现有顺序保证。把改进分成三个核心职责：

1. **一次检查阶段只做一次完整身份验证。** 先消除紧邻重复，保留初始验证、读取后验证、提交前验证各自的责任。
2. **组织 guard 协调当前准入／授权依赖，不兼做所有业务对象的总队列。** 已核查的普通操作共享保护；改变组织、成员、角色、任职、ACL、scope 或 MFA 策略的写者保持排他保护。未来 ApprovalIntent／Activation／QualificationFactSet 的读写协议需另行纳入，不能自动继承共享模式。
3. **业务并发单独协调。** 幂等键有自己的事务级同步，工作项有自己的读／写行锁与版本检查，账号变更继续由已有 principal 写锁协调。

第一版不新增人员／资源／组织三套 guard 表，不修改 HTTP 合同，不添加审批／流程模型，不引入外部权限服务。已有 trust、principal、组织 guard 和业务对象已能表达首轮所需的大部分冲突范围。

组织授权写者仍会暂时阻塞相关普通请求；这个取舍先保留。局部 ACL／任职写者进一步细化，以及整套组织 guard 从行锁换成 advisory 锁，依据首轮实测另行决定。

## 2. 源码暴露的真实开销

### 2.1 已有基线可以从调用链解释

当前 [HumanOperations.request](../app/providers/human.py#L70) 先通过独立身份事务签发 evidence，随后进入业务事务。保持这一边界可以让 JWT/JWKS 验证位于业务事务之外；不能为了省查询把网络验签搬到持锁事务内。

[CommandKernel.read](../app/execution/kernel.py#L333) 前后各调用一次 `_authorize()`。每次 `_authorize()`：

- [AuthenticatedAuthorizationLoader.load](../app/authz/authenticated.py#L65) 已调用一次 `validate_human()`；
- kernel 随后又经 `identity_validator.validate_before_commit()` 调用一次 `validate_human()`。

因此，读取业务事务内完整身份复核 **4 次**；写命令初始阶段 **2 次**，flush 后提交前另 **1 次**。前置认证事务另有一次完整原生会话检查。

对既有容量 fixture（单层 scope、无关联 browser session），静态调用计数对应如下。它解释的是已测的 107/85，复杂 scope 或真实 SSO 会增加 SQL。

| 部分 | 当前 SQL | 说明 |
|---|---:|---|
| 前置 authenticate_human | 22 | 来源快照、候选映射、锁内当前身份／MFA／原生会话 |
| 业务 coordinator | 3 | trust、principal、loader 对 principal 的重复锁读取 |
| kernel 组织 guard | 1 | 每个业务事务入口 |
| 一次授权 facts load | 14 | guard 重取、当前授权及工作项；此 fixture 一层 scope |
| 一次 validate_human | 13 | profile/provider/trust 3；principal/external/root/member/binding 5；MFA 1；native account/session/client/principal 4 |
| 工作项只读 DTO | 1 | 从当前业务事务投影 |

读取：`22 + 3 + 1 + 2 × (14 + 13 + 13) + 1 = 107`。

编辑：`22 + 3 + 1 + 幂等查询1 + (14 + 13 + 13) + execution插入1 + handler读取1 + flush写入3 + 最终身份13 = 85`。

### 2.2 首轮去重方案应比“事务内全缓存”更小

建议将授权阶段的职责调整为：

1. loader 构造当前授权事实并校验请求绑定，记录该事务已完成 facts load。
2. kernel 在事实与请求对应关系核对后，**在决定允许／拒绝以及披露版本冲突之前**，强制执行一次完整 identity validator。
3. 重新采样服务器时间、检查 evidence／请求期限，再执行 AuthorizationEngine。
4. 写命令仍在 handler、flush 后完整复核身份；读操作仍在 reader 后再次完整走授权阶段。

具体只调整 [authz/authenticated.py](../app/authz/authenticated.py)、[execution/kernel.py](../app/execution/kernel.py) 和相关接口说明。现有 loaded marker 只能表示“这个事务已加载 facts”，不能被重新解释成永久有效的身份验证票据。所有携带 authentication 的 kernel 仍必须装配 coordinator 与 identity_validator；装配入口继续为 [create_human_kernel](../app/providers/human.py#L41)。

当前生产代码中 AuthenticatedAuthorizationLoader 只在该工厂创建，另外两个构造点位于测试支持与 Runtime 集成测试，没有独立绕过 kernel 的生产授权入口。本结论须在实际修改时重新核对。

同一容量 fixture 的**静态预估**为：读取 `107→81`，编辑 `85→72`。这不是新跑出的性能结果，也不是所有请求的固定 SQL 数；仍需重跑基线。

不能进一步直接删掉最终数据库身份验证。现有 [test_identity_change_inside_handler_cannot_commit_even_after_initial_authorization](../tests/execution/test_authenticated_commands.py#L193) 明确覆盖：handler 在同一事务里误改 ExternalIdentity，最终必须拒绝并全部回滚。其他事务被锁阻挡，不能证明本事务没有误改安全事实。

后续批量读取 profile/provider/trust、principal/membership/binding 可以另作小改动，但要保留缺失行、状态、版本、环境、来源与到期错误语义。第一批不把最终校验替换成“只比较缓存 deadline”。

## 3. 组织 guard 改成明确的授权读写协议

### 3.1 推荐的小型实现边界

建议新增窄的 `app/storage/coordination.py`，集中两个实际需要的能力：组织授权共享／排他保护，以及命令幂等键事务同步。不是新运行时、策略 DSL 或通用锁框架。

组织保护模式由受信任入口指定，不来自 HTTP body、角色名或 Agent 输出。迁移过程中旧 `lock_tenant()` 可暂保留为显式排他兼容入口，尚未审计的路径继续用原协议；不能把它的默认行为全局翻成共享。本轮关闭前，所有调用者应迁到明确模式，兼容别名退出；Outbox 等保留排他行为的路径也必须显式声明，不能靠旧默认值维持安全。

原提案先选择现有guard行的 `FOR SHARE` / `FOR UPDATE`。实施时含MFA写者的负载证实共享行锁后发读者会超越等待中的写者，独立PG对照后改为：统一入口先取得同组织事务级advisory S/X准入门，再取得原guard行S/X。保留原行锁与持久guard语义，以数据库协调改善写者可达性；所有调用方使用同一组合协议，不能出现读者与写者采用不相交锁的情况。实际证据见[实现复查](reviews/concurrency-optimization-2026-10-26.md)。

记录已持锁模式时，必须绑定当前 session 的**准确根事务**及组织，不能复用到下一事务、另一个请求、另一组织或 savepoint 回滚之后。第一版不开放持锁期间嵌套事务／隐式锁升级；同一事务需要写者模式时，从入口直接取得 X。内部 loader 应确认已持保护，不再悄悄重取 X。[loader 当前重取位置](../app/authz/loader.py#L244)

### 3.2 具体冲突矩阵

S 表示共享保护，X 表示排他保护；控制面本地操作外层的 `acquire_admin_lock()` 保留，它不参与日常业务流量。

| 代码路径／操作 | 首轮目标锁序 | 保留的局部责任 |
|---|---|---|
| IdentityService 真实身份解析 | trust S → principal S → org S | 锁后重读来源／映射／成员／MFA／会话 |
| 本人任职发现 | trust S → principal S → org S | 同一明确成员经历；返回前验证 |
| 工作项查询 | trust S → principal S → org S → work S | 查询 DTO 物化；reader 后完整重验 |
| 普通工作项编辑 | 幂等事务锁 → trust S → principal S → org S → work X | 单对象版本、幂等、审计、最终重验和原子提交 |
| 原键结果恢复 | 同一幂等事务锁 → trust S → principal S → org S → work S | 等待先行同键事务结束后重新读库和授权 |
| UserInfo／本人 session、consent 列表 | trust S → principal S → org S；必要 account S | 明确纯查询入口，不消费凭据或修改状态 |
| 密码／OTP／恢复／refresh／session 撤销／SSO 推进 | trust S → principal X → org S → 原 account/session/request 写锁 | 一次性消费、凭据版本、会话与同意状态修改 |
| RP 退出确认 | trust S → principal X → org S → browser X → logout request X | 持有浏览器证明、版本、消费和截止；无浏览器的独立 challenge 路径保留原逻辑 |
| 人员停用 | principal X | 已有局部协调，无须新增 org X |
| 组织根、成员经历撤销 | 原 principal S（适用时）→ org X | 当前版本和组织 epoch |
| 角色／任职／ACL／资源／scope 管理 | 原有按序 principal S → org X → 必要 work X | 发布内容、授权集合变动和版本检查 |
| 组织 MFA 修改 | 按序 trust S → org X | 原子更新策略、版本、epoch、journal |
| 原生 client 配置／禁用／准入 | trust X → org X → client／批量 session 修改 | 全部既有 client 失效与批量撤销保证 |
| profile 撤销 | trust X → profile X | 当前来源失效 |
| 原生安装 | trust X → principal X → org X | 显式绑定已有人员；管理幂等与 journal |
| 内部 get_result(execution_id) | 暂保留原 org X 路径 | 未提交记录不可见时的既有等待语义 |
| 未默认挂载的 OutboxWorker | 暂保留 org X → outbox/attempt | 领取、重试预算、租约、fencing 与回执；单独优化 |

组织授权读者之间可以并行；授权写者与读者仍互斥。改变授权事实的未来业务命令必须明确使用管理／写者协议，不能伪装成普通标题编辑命令。

### 3.3 现有 principal 与 trust 已提供额外保护

[credentials.acquire_account_lock](../app/native_auth/credentials.py#L67) 汇聚大多数原生凭据、账号和会话写者，它们持有 principal X；[logout._lock_browser](../app/native_auth/logout.py#L152) 也按同样顺序取得 principal X。普通身份与业务持有 principal S，所以改变组织 guard 模式后，这些身份状态仍有明确冲突。

client 配置可批量修改多个账号的 session，未逐个取得 principal X，但已经持有 trust X，与普通路径的 trust S 协调。[client 管理锁入口](../app/native_auth/client_administration.py#L138)

首轮依赖这些已有边界，保持账号与 principal 关联不可变，以及所有安全事实写者通过受控入口修改。不应把锁藏进 [current_session](../app/native_auth/current_session.py#L39)，导致调用方不知情的锁顺序变化。

组织 guard 还保护授权集合的不存在事实及新插入关系。先保留它，避免只锁现存 ACL 行而漏掉新增事实。已有 org epoch 只是部分集合版本与审计证据，不能覆盖 principal/trust/client/session 所有失效源。[版本证据说明](../app/authz/loader.py#L402)

## 4. 幂等与结果恢复必须先补齐

### 4.1 精确复用既有幂等身份

锁的规范输入与现有 SQL 唯一约束一致：

```text
(organization_ref, actor_ref, command_name, command_version, idempotency_key)
```

**不能加入 target、payload、assignment、context digest 或 request_id。** 同一个幂等键被用于不同目标或参数时，也必须先进入同一个同步范围，然后按原协议检查冲突。[持久唯一约束](../app/models/execution.py#L26)

对于**当前单数据库事务完成的短命令**，推荐使用 PostgreSQL **事务级 advisory 排他锁**表达这个尚未有持久记录的键：第一条命令记录不存在时，单纯行锁锁不到它。规范编码带固定命名空间／版本，稳定摘要映射到数据库锁键，禁止 Python 进程随机 `hash()`；摘要碰撞只能增加等待，持久唯一约束和完整 tuple 比对仍是正确性依据。首轮无需新增幂等占位表。该锁不是未来 accepted/running/attempt 的持久执行所有权，不能跨 WASM、网络调用或人工等待持有。

### 4.2 固定顺序与等待语义

推荐在**命令事务**的 trust／principal／org 锁之前取得幂等锁。HumanOperations 的前置认证事务此时已经结束；排队者不会因为等待同键，就提前持有 org S 延长撤权等待。

`execute()` 与 `get_result_by_key()` 使用同一顺序，且每个事务只允许一个命令键；禁止在普通 handler 内嵌套启动另一个受控命令。其他控制／认证路径不取得这把幂等锁。正式实现必须用竞争测试验证没有任何反向路径，等待后重新检查期限和身份。

成功取得锁后，用 READ COMMITTED 的新语句查幂等行：

- 前序已提交：按**当前读取权限**重放；仍先做读取授权，再披露 payload/context 冲突。
- 前序回滚：`execute()` 可以按原命令流程执行；`get_result_by_key()` 仍是纯查询，未找到结果时返回既有 not-found，不自动执行命令。
- 锁等待超时／取消：返回既有暂不可用语义并释放资源，不能误报操作不存在。
- COMMIT 回执丢失：仍返回 unknown 和原键恢复位置，不把网络错误解释成未执行。

`get_result(execution_id)` 在记录未提交时无法反推原幂等键。它是内部接口，当前公开 HTTP 恢复使用原键路径；首轮保留其 org X 等待行为，避免额外引入第二套 execution-id 锁或悄悄改变合同。[公开恢复说明](../app/api/work.py#L135)

原键查询等待的是已经取得该键同步权的先行事务。如果查询先取得同步权而写请求尚未到达，未找到记录仍只是本次查询没有可见结果，不能作为未来不会执行的证明。客户端重试继续使用原键；对于当前短命令，若出现不符合其提交合同的持久 running 记录，保留 unknown，不自动重跑。未来分阶段评估允许的 accepted/running 必须按独立、版本化的持久状态机解释，不能沿用短命令的异常推断。

## 5. 工作项和账号读写分别明确模式

[DatabaseAuthorizationLoader](../app/authz/loader.py#L345) 当前对工作项一律 `FOR UPDATE`，包括查询。组织 guard 改 S 后，需要同时将查询／结果读取改 work S；写命令保留 work X，取得锁后再读取资源状态并判定权限。

对象模式由 kernel 的读／写入口及幂等重放分支明确决定，不通过 action 名称猜测，也不接受客户端指定。`execute()` 命中已完成记录时实际走结果读取，其对象保护可以是 S；没有命中才进入 work X 的领域修改。

[update_work_details](../app/features/work/details.py#L80) 的本地 expected_version／状态校验继续保留。现有对象锁已经能安全支持不同对象并行，首轮无需同时改为 CAS；条件 UPDATE 可以作为后续独立实验，不能与授权锁改造混成一次不可定位的变化。

账号列表和 UserInfo 现在复用账号写锁路径。为纯查询提供明确读模式，使同一人的多个只读请求也可并行；refresh、OTP、恢复、consent 推进与退出仍保留 principal X。`fresh` 只代表认证新鲜度，**不能被当成读／写模式**。[AccountSessions._actor](../app/native_auth/sessions.py#L61)

原生 authorize 的 client S＋登录 challenge 写入是独立短事务，结束后才进入人员锁链；logout prepare 也有独立边界。保持这些分段，不能为了统一接口先持 client/request 锁，再倒过来取得 trust/principal。

## 6. 文件级改动清单

| 文件 | 计划改动 | 不变量 |
|---|---|---|
| `app/authz/authenticated.py` | 去掉 load 内紧邻重复的身份全量验证，明确 loaded marker 语义 | 绑定准确事务与 evidence；不能直接把 facts 当允许票据 |
| `app/execution/kernel.py` | 身份验证移到决定和冲突披露前；补幂等同步；选择 org/work 模式 | 最终完整身份验证、幂等／审计／unknown 保留 |
| `app/execution/contracts.py` | 明确加载、验证、锁协调责任及受信任的读写模式传递 | 请求不能指定可信锁模式；不加入任意配置 DSL |
| `app/storage/coordination.py`（拟新增） | 小型事务级组织保护与幂等键同步 | 固定锁序、事务绑定、禁止升级与旧证明复用 |
| `app/execution/storage.py` | 兼容原排他 helper；UoW 事务所有权不变 | 清理完成后才释放连接／租约，取消不伪装成功 |
| `app/authz/loader.py` | 确认已持组织保护，区分对象读写锁；后续按需批量查询 | 单任职、scope、role digest、direct ACL、对象状态全部保留 |
| `app/identity/service.py` | 普通认证事务选择 org S；继续锁后新快照复核 | JWKS／网络验证不在持锁事务中 |
| `app/providers/human.py`、`assignments.py` | 公共装配传递受信模式，任职查询使用 S | 业务模块继续复用入口，不重新复制安全装配 |
| `app/native_auth/credentials.py` | 明确只读入口；实际凭据修改仍 principal X，org S | 一次性消费、账号失败预算、凭据版本及撤销 |
| `app/native_auth/oauth.py`、`sessions.py`、`logout.py` | 按实际操作选择读／写保护；保持独立 challenge 边界 | scope、client 版本、CSRF、MFA 新鲜度与截止语义不变 |
| identity/authz/native 管理模块 | 显式标注写者 X，避免依赖默认模式 | 管理 mutex、environment、journal、版本／epoch 同事务 |
| `app/delivery/worker.py` | 此轮只标明继续使用旧 X 协议 | 不把未启用 Worker 的潜在收益计入业务基线 |

目前不需要数据库迁移、额外业务表、公开 API 大版本或新的配置服务。历史 `tenant_*` 物理名仍表示固定组织根；性能整理不夹带表名迁移。

## 7. 按可独立验证的批次推进

| 批次 | 交付内容 | 通过条件 |
|---|---|---|
| 0 · 路线合同对齐 | 明确短命令／分阶段执行、锁序和过渡项退出条件 | 本文与一期、WASM、业务切片、核心对象及路线文档一致；此批只有设计产物 |
| A · 验证职责去重 | 每个授权阶段只一次完整身份验证；提交／披露前验证保留 | 现有身份失效、同事务误写、版本错误披露、到期边界均通过；原 fixture SQL 预估81/72经实测核对 |
| B · 组织读写协议与查询并发 | 小型模式协议、持锁断言；身份／任职／UserInfo／账号列表查询先放开 S | 不同人及同人纯读能真实重叠；写者仍阻挡相关读者；无S→X升级 |
| C · 幂等同步与业务并发 | 同键同步和原键恢复先完成，再切普通 kernel 为 org S、work S/X | 同键只执行一次；同对象冲突明确；不同对象命令确实并行；撤权顺序不变 |
| D · 原生认证写路径 | 保留 principal X，缩小组织保护为 S；完成所有实际写者核对 | 不同人员 refresh／凭据操作可并行；同人的一次性消费／撤销竞争正确 |
| E · 集成与容量复验 | 完整 PG、Chromium、Runtime、合同，重跑混合基线和含管理写者的负载 | 功能无回退；测出延迟/吞吐/撤权等待改善，提交可复现证据 |

每批先通过正确性验证，再比较性能；旧排他行为保持可识别的回退边界，兼容 helper 按第11节退出。若实验选择组织 advisory 锁，应作为整套协调后端的单独变更，不在同一实例并存互不冲突的协议。当前单 worker 的切换按第12节停接入并排空，不借本次性能整理宣称滚动升级已就绪。

暂不同时推进跨请求权限缓存、合并前置认证与业务事务、同人多 session 写并行、局部 ACL 多级 guard、client trust 细分、Outbox SKIP LOCKED 或新的流程引擎。它们只有在首轮数据仍显示相应瓶颈时才进入下一轮。

## 8. 必须具备的验收证据

### 正确性：使用真实 PostgreSQL 和显式同步事件

- 两个普通读取／不同工作项命令能够在受保护区重叠，避免仅以总耗时猜测并行。
- 相同对象的读写冲突与版本更新顺序正确，多个读取可并行。
- 同键同参数只发生一次领域修改、一条 execution／audit；同键不同目标／payload／任职遵守原冲突与信息披露规则。
- 原键查询等待先行提交，回滚后不会伪造成功；commit reply loss 可用新认证恢复；等待中超时／取消不泄漏锁。
- 对每类写者测试“撤权先行／业务先行”：人员、成员、任职、角色、ACL、scope/resource、MFA、profile、client、session/browser、密码／因素重置。
- ACL／策略不存在后新增等集合变化仍被保护；旧 evidence 不能因组织 epoch 相同绕过 principal／trust／session 变更。
- handler 同事务误改身份映射仍无法提交；reader／handler 结束根事务仍被拒绝。
- reader 之后、最终 flush 之后发生 token、MFA、任职、关系或请求期限到期仍拒绝；服务器最终时刻重新采样。
- 持锁模式不能跨事务复用，不能隐式升级；有排他写者排队时观察新请求与取消行为，不假设数据库提供特定公平调度。
- 默认未装配的 delivery 全部旧回归继续通过，禁止因共用 helper 默认变化破坏 lease／attempt／unknown。

现成入口包括 [身份写命令回归](../tests/execution/test_authenticated_commands.py)、[受保护读](../tests/execution/test_protected_reads.py)、[原生读取到期](../tests/native_auth/test_audit_identity.py)、[组织 MFA 竞争](../tests/identity/test_assurance_concurrency.py)、[原生撤销竞争](../tests/native_auth/test_command_concurrency.py)与 [PG 阻塞观察工具](../tests/support/postgres.py)。SQLite 继续快测，但不承担共享锁正确性证明。

### 性能：保留旧基线，补含写者的测量

使用[现有混合基线](reviews/p2p3-capacity-2026-10-26.md)相同人员、对象、scope、连接池与 1/4/8/16 并发，分别记录每类操作的 SQL、吞吐、p50/p95/p99、超时和错误。额外加入权限撤销／MFA 变更的写者等待、同人多会话与同键重试，不仅测试没有授权变更的理想读负载。

现有测量器仅识别组织 `FOR UPDATE`／SQLite UPDATE。加入共享模式／advisory 后，必须同步更新识别和事务归属，分别统计 org、principal、对象和幂等等待，不能因为新锁没被采集就报告等待为零。[测量入口](../scripts/benchmark_mixed_auth_work.py#L90)

首轮用“不同对象确实并行、撤权顺序完整、相同负载下延迟下降”验收；当前没有业务提供的生产 SLO，不提前承诺固定 QPS 或提升倍数。

## 9. 本轮复核范围

本提案有两条独立只读交叉核对：一条覆盖身份／原生／管理写者锁序；另一条覆盖 kernel／loader 的重复验证、幂等和 Outbox 隐含协议。没有运行新的容量实验，也没有将静态计数推导标成已完成优化。

代码与来源指纹见[提案证据](reviews/stage-concurrency-plan-evidence-2026-10-26.json)。上文路径、类名和行号用于说明现有代码与拟修改位置；新模块、锁模式及目标 SQL 数均属于提案。

<a id="route-compatibility"></a>
## 10. 与开发路线的衔接

这一轮属于 P1b 已实现底座的有限整理，不新增产品阶段，也不代替 Maestro 实际客户端验收。P1c/P1d 的接口设计可以同步推进；其事务实现接入时以已经通过验收的协调协议为基础，不能把全部后续开发无限期阻塞在吞吐调优上。

| 路线 | 本轮必须保证的接缝 | 对应阶段再实现的能力 |
|---|---|---|
| P1b 人员身份与权限 | 原 API、单任职、认证强度、撤权顺序、原键恢复、审计语义稳定 | 正式资料／入离职／权限管理产品入口及 Maestro 实际交互 |
| P1c WASM 与签名准入 | DB Session、事务锁和持锁证明不进入 runner；平台统一拥有协调协议 | ABI、隔离预算、精确批准意图、签名／激活／撤销；新增 writer 各自申明保护范围 |
| P1d EvaluateQualification | 短事务读取快照→锁外计算→短事务重验提交；稳定逻辑幂等身份贯穿 | 持久 execution/attempt 状态、条件终态、旧尝试回执拒绝、FactSet 集合 revision；不能拖到 P4 才保护旧 attempt |
| P2 人员任务与容量 | 对象锁、容量不变量、权限关系写者分别明确；命令 ID/version 不依赖 Python 类名 | CreateTask/Claim/Delivery/Review 的动作和关系写者协议；不能认为所有“业务命令”都可 org S |
| P2 功能组合与停用 | 组件租约与业务事务分离；受信任模块不能自行更换协调规则 | 新注册／卸载前必须保留历史结果读取、原键恢复与 unknown 对账的可用实现 |
| P3 持久流程 | 人工等待只保存持久引用；恢复时重新取租约、重新认证授权 | run/step/attempt、checkpoint、定义版本固定与迁移；不保存当前 Session／锁／短期允许结果 |
| P4 多进程与多节点 | 使用同一 PostgreSQL 的事务同步可跨进程，但不等于任务执行权或外部 exactly-once | 跨节点租约、fencing、旧 worker 回写拒绝、混合版本和故障接管实测 |
| P5 扩展能力 | 同一可信命令边界继续约束 hostcall／连接器 | WIT、SDK 等以实际消费者和独立威胁模型为前提 |

### 10.1 短命令与分阶段执行不能混为一类

当前 `CommandKernel.execute()` 的 handler 只做有限本地事务操作，不能持锁调用 WASM、签名服务、网络或等待人员。未来 EvaluateQualification 使用相同逻辑幂等身份，但“受理、计算、最终提交”是不同阶段：每个数据库阶段短持必要保护，阶段之间释放连接和数据库锁；由持久 execution/attempt 身份和条件更新控制谁有权提交。

WASM 前后的依赖复核包含当前人员／任职、原始输入摘要、对象版本、批准／激活版本、制品撤销状态、FactSet revision 与最早失效时间。初次快照不能先读旧数据后读新 revision；空集合、新增／删除、跨 scope 移动都必须有完整 writer 协议。计算成功不等于提交成功，事务锁释放也不证明旧 runner 已停止。

P1d 首次引入可持久化的 accepted/running 时，应明确迁移和响应状态兼容，建立恢复、取消、重复回执和旧 attempt 终态拒绝测试。现在不预建这些表，也不允许把现有 `running/succeeded` 检查约束的修改当作完整状态机交付。

### 10.2 锁序只有一份权威定义

统一目标顺序以[一期事务协议](phase-1.md#transaction-protocol)为准：需要逻辑入口互斥的短事务先取其键 mutex，随后 TrustState → Principal → 组织保护 → 批准/激活/事实/领域对象 → 持久 execution/结果行。入口的逻辑幂等 mutex 与最后的 CommandExecution 行不是同一把锁。

同类对象排序、多对象容量检查、缺失集合保护和新 writer 的模式在其功能进入实现前冻结。`CreateTask` 创建 owner/reviewer 关系、未来审批改变有效授权等路径，必须先按安全事实写者评审，不能因名称属于任务域就自动使用 S。当前 CommandSpec 没有提供随意修改安全事实的授权能力；普通 handler 的禁止规则继续保留，未来确有需求时采用独立受控入口和状态协议。

<a id="transition-exits"></a>
## 11. 过渡项与明确退出条件

目标是避免已知协议缺口和无期限兼容分支。后置的能力保持明确范围，不为消除所有未来 TODO 而提前实现尚无消费者的框架。

| 项目 | 负责模块／本轮处理 | 退出或再次评审的触发条件 | 禁止形成的长期依赖 |
|---|---|---|---|
| `lock_tenant()` 旧默认 X 别名 | storage coordination／各调用方；迁移期临时保留 | 本轮 E 批关闭前生产、工具与测试均使用明确模式，移除别名；AST/调用清单验证无残留 | 新模块依赖隐含排他默认值 |
| 内部 `get_result(execution_id)` 的 org X | execution/kernel、providers/execution；保留明确且受测的兼容语义 | 在成为默认 HTTP 查询、持久评估恢复入口或新增生产消费者之前，必须完成按执行记录的局部恢复协议 | 新功能继续借全组织互斥等待 UUID 结果；提前删除已有 Python facade |
| Outbox 现有 org X | delivery/worker；迁到显式 X，保留全部 lease/attempt 回归 | 首个真实连接器／生产 Worker 启用之前，单独验收领取、回执、unknown、撤权与多 worker 竞争；是否需 SKIP LOCKED 由数据决定 | helper 改默认后未经验证放开投递；因后来撤权而丢弃已经发生的外部回执 |
| 通用 commands provider／动态命令注册 | providers/execution、未来能力装配；保持当前受信注入边界 | 新生产消费者启用前核对身份/storage/handler 依赖租约；动态替换前具备历史结果与 unknown 对账的可用读实现 | 只声明 STORAGE 就假定注入的身份服务也受生命周期保护；卸载 handler 后原结果不可恢复 |
| 同人凭据修改 principal X、client 配置 trust X | native_auth／identity；作为当前明确协调范围保留 | 同人多会话或频繁 client 管理成为实测热点时，再审计细化后的所有写者 | 为追求吞吐绕过一次性消费、批量撤销或来源变更 |
| 未来多阶段命令状态 | execution＋资格评估应用层 | P1d 第一条锁外计算路径上线前完成持久状态、迁移、恢复和旧 attempt 条件提交 | 将事务 advisory 锁当持久 lease，或按短命令协议自动重跑 running |
| SQL 数量目标 | benchmark 与验证记录 | A 批用同一 fixture 实测；E 批再覆盖真实 SSO、不同 scope 深度和授权写者 | 把81/72静态估计写成生产SLO，或为达计数删安全检查 |
| 事实缓存／多级 guard | identity/authz；当前不引入 | 完整失效源与 writer 目录、收益实测、竞争证明齐备后另案 | 把一个 org epoch 当全局授权有效期 |

每个触发点都应在进入下一阶段时检查；若兼容约束仍存在，验收记录必须说明范围，不能记作相应能力已经完成。模块 owner 是代码责任边界，不是虚构某位人员已接受任务。

<a id="coordination-upgrade"></a>
## 12. 协调协议、部署与停止验收

- 冻结逻辑幂等身份、锁键命名空间、规范编码和摘要映射。编程语言升级、进程重启、handler 类名或组件代次变化不能改变同一操作的物理锁键；业务摘要与锁键分别管理。
- 行锁的 S/X 使用同一 guard 行，具有数据库定义的冲突关系；不同 advisory 编码版本或行锁／advisory 两套协议可能完全不相交。唯一约束只能兜底记录唯一性，不能替代等待、结果恢复和撤权协议。
- 当前单 worker 采用停止新接入、排空在途事务与服务调用、切换确切制品、恢复就绪。不能一边执行旧锁键协议，一边仅靠配置热切到新协议；回退同样需要排空。
- 未来滚动升级必须先证明新旧版本共同协调，或采取明确的维护窗口；现在不编造一个尚未验收的“通用双协议兼容层”。数据库协调不进入纯 `app/runtime`，Runtime 只继续负责资源/调用排空。
- 增加验收：在幂等锁等待、对象锁等待和提交清理阶段停止／取消，确认事务完成释放后才退出组件；重启后新请求仍通过同键找到持久结果。未知提交、外部副作用、旧 attempt 回写各按自身协议恢复。
- 本轮交付冻结前同时核对代码、API/数据库合同、阶段文档、锁调用清单和实测基线；相关规格不能继续保留“所有普通业务永久 org X”或“幂等逻辑锁在业务对象之后”的相互冲突指示。
