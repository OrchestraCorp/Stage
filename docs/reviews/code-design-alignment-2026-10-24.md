---
title: Stage 代码与设计目标对齐审查
status: review-record
policy_effect: none
review_date: 2026-10-24
baseline_commit: eb11e4371ce212629ba9dee3bbe811ebd006ca48
---

# Stage 代码与设计目标对齐审查

当前实现与“统一授权入口、事务提交、幂等、审计、可靠投递”的方向一致，已形成可测试的内部执行底座。P1 的自研组件 Core、真实 IAM、签名准入、WASM 和资格评估联调仍未实现。下一步应围绕这些共同契约扩展，并先解决已发现的投递问题；现有底座可以继续使用。

本次检查基于上述提交，开始时 `main` 工作区干净。范围包含 `app/`、测试、迁移、契约、CI 和 P1 设计。只新增审查记录与入口链接，未修改应用代码、依赖、数据库或外部 API。云设备负责 Stage；Maestro 在本地研发，接口交付以 Stage 已实现并验证的契约为准。

## 1. 实际验证

在当前 Linux 云开发设备的 Python 3.12 虚拟环境中执行：

| 检查 | 结果 | 能证明的范围 |
|---|---|---|
| `.venv/bin/python -m pytest -q` | **192 passed，35.48s** | 现有单元、内部执行/投递、SQLite 存储和迁移测试 |
| `.venv/bin/python -m ruff check app tests scripts` | 通过 | 当前代码静态规则 |
| `.venv/bin/python -m ruff format --check app tests scripts` | 通过，68 files | 当前代码格式 |
| `.venv/bin/python -m mypy app tests scripts` | 通过，68 source files | 当前类型检查范围 |
| `STAGE_ENV=test .venv/bin/python scripts/check_contract.py --check` | 通过 | 当前 OpenAPI 与实现一致 |
| `git diff --check` | 通过 | 空白检查 |

额外使用临时 SQLite 和虚构数据复现了第 2 节的投递问题，并直接调用纯授权引擎复现了第 3 节的策略缺口。这些探针没有加入上述 192 项测试数。

本次未运行真实 MySQL 8.4 / PostgreSQL 17、容器构建、远端流水线或真实身份/模型服务。仓库提供的是 [GitLab CI](../../.gitlab-ci.yml)，`origin` 指向 GitHub，未提供 GitHub Actions 工作流；还需核实流水线实际承载位置并取得真库执行证据。不能将 SQLite 的串行写行为视为生产库锁与撤销竞争的证明。

## 2. 当前实现中可复现的问题

### R1 · P2：回执字段类型错误会使投递校验抛异常

位置：`app/delivery/worker.py:397–403`，调用处 `:420`、`:481`；[回执契约](../../app/delivery/contracts.py) 是普通 dataclass。

触发：sink adapter 返回 `DeliveryReceipt`，其中消息和去重身份正确，但将外部回执编号映射成整数 `receipt_ref=42`。dataclass 不执行运行时类型验证，`_receipt_matches()` 调用 `.strip()` 时抛 `AttributeError`，越过已有的 `receipt.invalid → unknown` 分支。

临时集成探针观测：

```text
exception = AttributeError: 'int' object has no attribute 'strip'
receiver effects = 1
message status = delivering
attempt status = delivering
```

外部效果已发生，本地只能等租约过期再恢复；若通过后台轮询运行，还会触发 R2。现有测试检查错误消息 ID，未覆盖回执字段类型错误。这是 adapter 错误处理的稳健性问题，目前没有已暴露的外部利用入口。

修正方向：让回执校验对不合法类型返回否定结果，或在 adapter 边界进行严格解析并统一转换为未知结果；`send` 和 `reconcile` 都应覆盖该路径。保留稳定去重键，不因格式错误重新发送。

### R2 · P2：后台轮询异常退出后没有及时的宿主故障信号

位置：`app/delivery/worker.py:541–558`，错误直到 `close()` 的 `:581–587` 才通过公开调用取出。

触发：启动后台 worker，在一次 UoW 提交处注入临时数据库异常，随后恢复数据库。`poll()` 的 `run_once()` 异常使后台 task 结束，状态仍显示 `_stopping=False`，后续消息不再投递，再次 `start()` 又因保留 `_loop_task` 而拒绝。

临时集成探针观测：

```text
poll_task_done = True
poll_error = OSError
stopping = False
sink_calls_after_recovery = 0
restart = RuntimeError: delivery worker already started
```

失败后停止是可接受策略，但宿主必须立即获知失败，才能监督、告警或重建组件。当前只能在关闭时发现错误，不满足设计中的运行期故障可观察性。现有后台测试只覆盖正常启动/关闭。

修正方向：提供可等待的运行句柄或明确故障通知/状态，接入 Core 的组件监督；区分可恢复操作异常与组件终止，明确重试和重建规则。可选通知组件失败应呈现积压/降级，不把已提交业务命令改成失败，也不隐式令所有业务不可用。

## 3. 接入业务前必须补齐的授权约束

以下是现有策略与最新设计之间的缺口。任务 handler/API 尚未实现，不能将这些纯函数允许判定描述成已经上线的越权漏洞。

| 场景 | 当前可复现结果 | 设计要求与落点 |
|---|---|---|
| 同一人既是 `reviewer_of` 又是 `assignee_of`，选择 department-owner 任职，状态/版本有效 | `task.acceptance.review → allowed` | [业务切片](../business-slice-plan.md)要求实际责任主体、交付提交者和有效代表主体不得自验收。当前策略仅检查正向关系，需要可信冲突事实和统一拒绝规则 |
| Agent 持有有效验收委托，委托人具有 reviewer 关系和岗位能力 | `task.acceptance.review → allowed` | 当前任务规格要求最终接受/驳回由人类 reviewer 完成；需按 action 限制主体类型 |
| 后台 SERVICE 使用普通 assignment | 进入普通任职授权路径 | [身份规范](../identity-and-authorization.md)已明确 P1 应新增 `ServiceGrant` 输入分支。正式 worker 不能靠虚构人类管理员任职获得权限 |

前两项的代码位置是 `app/authz/permissions.py:77–82`、`app/authz/decision.py:78–127,152–169`；第三项是 `decision.py:65–75,126–129`。任务约束在 P2 接入前完成；ServiceGrant 属 P1 身份基座。

已确认的正确行为也应保留：每次选择单一任职；Agent 只使用委托人的资源关系，不能拼接自身关系扩大委托；未知策略、跨组织、过期任职/委托、对象版本不匹配均拒绝。授权模块当前是纯判定，不构成真实认证或权威事实加载。

## 4. P1 目标与代码映射

| 目标 | 当前代码 | 对齐判断 |
|---|---|---|
| 组件 Core、owner/generation/lease、依赖换代 | 没有 `app/runtime/` 或 RuntimeHost；[main.py](../../app/main.py)直接创建和关闭数据库工厂 | P1a 尚未开始；`tests/runtime/` 测的是现有 HTTP/配置/数据库生命周期，不是 Core 34 场景 |
| 可信身份与当前授权 | [app/authz](../../app/authz/)纯判定、内部 ExecutionContext、AuthorizationLoader Protocol | 接口边界合理；缺真实认证 adapter、成员/任职/委托/ServiceGrant 的完整持久事实与 loader |
| 能力定义和实际 handler 绑定 | [CommandSpec](../../app/execution/contracts.py)固定 name/version 和 input/result model，Kernel 内存注册 | 尚无统一 CapabilitySpec、实现摘要、provider 代次和调用租约 |
| 事务、幂等、审计、outbox | [CommandKernel](../../app/execution/kernel.py)、[UnitOfWork](../../app/execution/storage.py)、执行模型和第三条迁移 | 第一版内部短命令路径已实现并测试，可继续复用 |
| 外部投递与恢复 | [OutboxWorker](../../app/delivery/worker.py)已有租约、attempt、fencing、unknown、显式 reconcile、drain | 第一版内部实现；先处理 R1/R2，再装配真实授权与测试 sink；尚未接入应用生命周期 |
| 签名批准、制品准入和激活 | 无 `app/release/`，无 TrustState/Activation 等持久对象 | P1c 待实现；内部 JSON 摘要编码明确不是发布签名/JCS 编码 |
| 受控 WASM 与资格评估 | 无 `app/wasm/`、`features/qualification/`、FactSnapshot/FactSet writer 协议 | P1c/P1d 待实现；不能把普通 handler 当 runner |
| 外部接口与就绪 | `/health`，`/api` router 为空；无 `/ready` | 当前只有存活契约。Maestro 业务联调依赖身份与对应业务接口完成 |

现有内部命令只提交成功的本地短事务。`running` 行与业务事务一起提交或回滚；尚不是可跨进程恢复的接受/计算/失败/取消状态机，投递 DeliveryAttempt 也不能替代命令/WASM ExecutionAttempt。

## 5. 扩展前需要对齐的结构

1. **执行分段。** 当前 `kernel.py:151` 获取 tenant guard，`:198` 在同一个 UoW 中执行 handler。WASM、签名、IdP 和外部网络不能放进这个 handler。资格评估需要“持久接受/一致快照 → 事务外计算 → 短事务重验与终结”，并定义 attempt、租约、回执和恢复协议。
2. **统一锁序。** 当前先锁 tenant guard 再调用 loader。P1 [锁序](../phase-1.md#transaction-protocol)要求 TrustState、全局主体在 tenant guard 之前。未来直接在现 loader 中增加前两类锁会倒置顺序；应在可信执行协调层先安排锁集合，再由 loader 读取，不靠各模块自行约定。
3. **可信事实模型。** 原任职/关系表仍是工程底板，缺少新 IAM 所需的租户键、来源和 revision 等字段；不能直接把测试 loader 接到 HTTP。成员、任职、服务授权、委托及全局主体写入，都要遵守共同撤销协议。
4. **Core 与存储边界。** 先用 provider 包装现有 engine/session factory；Core 不导入 FastAPI Request、业务 ORM 或角色。CommandKernel 保留事务所有权，Core 管理 provider 租约。worker 退出失败或 drain 未完成时，不能提前关闭其存储依赖。
5. **执行证据。** 当前 DeliveryAttempt 记录 worker、租约和结果，尚不记录 dispatch 时的 decision/policy/service-grant 依据。正式接入时应补充必要的授权引用；不能以最初业务命令的审计替代后续投递授权记录。
6. **验收与交付。** 把 Core、身份、WASM 和联调场景分别运行并记录；取得真实两库迁移/竞争证据。外部 API 只发布真实实现，区分 liveness、组件就绪和投递积压。

保留现有设计的其他边界：重试不更换逻辑幂等键；已有结果读取仍需当前授权；组件卸载不删除持久事实；流程固定版本与当前撤销状态分开；Graphon/Dify 仅为参考；Stage 不扩展为 MaaS 或客户端模型配置平台。

## 6. 建议实施顺序

1. 修复 R1，补齐 R2 的故障监督接口及对应回归场景；确定真实数据库/CI 验收方式。
2. 按 P1a 固定 Core 公共接口、能力绑定和共同锁序，实现数据库 provider 与生命周期一致性测试；保留现有内部命令/投递测试。
3. 按 P1b 建立真实协议 adapter、隔离测试 IdP、可信身份加载与持久 ServiceGrant/委托，贯通当前授权和撤销竞争。
4. 按 P1c 实现冻结的 manifest 编码、批准/签名/激活和独立纯计算 runner，明确引擎版本、ABI、OS 隔离与有限预算。
5. 按 P1d 贯通 EvaluateQualification，验证快照集合变化、撤销、超时、重复请求、未知提交与外部回执丢失，再交付相应 Maestro 接口。
6. P2 接入完整任务业务时补齐独立验收与人类最终验收约束；通用持久流程仍按 P3 推进。

上述为本次代码审查的建议顺序，不将尚未冻结的认证 profile、密码方案或预算数值写成已批准选型。
