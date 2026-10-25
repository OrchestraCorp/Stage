---
title: Z Pool 工程能力在 Stage P1 的复用范围
status: implementation-planning
implementation_status: not-started
policy_effect: none
review_date: 2026-10-24
---

# Z Pool 工程能力在 Stage P1 的复用范围

有必要吸收 Z Pool 已有的工程实现，优先覆盖一期必需的事务、幂等、审计、运行资源和故障处理。采用方式是按 Stage 对象和契约提炼，再在 Stage 的场景中验收；不以原项目有代码或历史测试记录替代目标系统验证。

本记录回答复用必要性与范围。2026-10-24 已落地第一批 Stage 内部执行、审计、幂等、outbox 和 delivery worker 代码；真实身份、受控 WASM、制品批准和准入仍未实现。源版本、源码位置及已确认差异见[架构对照](z-pool-architecture-comparison.md)，一期交付仍以[基座方案](phase-1.md)为准。

## 1. 应继续使用的现有基础

Stage 已有 FastAPI 应用工厂、请求ID、错误脱敏、异步数据库工厂、UTC/精确字符串类型、数据库URL校验、迁移骨架、OpenAPI契约检查和纯授权判定。这些应继续使用、按需要扩展，不因寻找 Z Pool 复用点而重建第二套实现。

已有 Redis 依赖不意味着已经有可靠队列；当前 Stage delivery worker 使用数据库租约/attempt/unknown 对账，尚未接入外部 sink 或 HTTP。旧 AuditEntry、IdempotencyKey、OutboxMessage 结构仍是历史轻量结构；新增实现位于 `app/execution/` 和 `app/delivery/`，必须按对象的实际边界判断缺口。

2026-10-25 已确定 PostgreSQL 17 为唯一长期主库、Redis 8 为辅助存储，见[存储策略](storage-strategy.md)。不再迁入 MySQL 兼容要求；历史迁移和原项目评审证据保留。Redis 配置/client factory 与业务接入分别验收，不能据依赖或探针将 Z Pool 的队列、缓存或协调保证搬到 Stage。

## 2. P1 优先提炼的能力

路径为各仓库的相对路径；目标模块为规划边界，不表示已创建。

| 优先项 | Z Pool 实际参考 | Stage 落点与必须增加的语义 | 验收要点 |
|---|---|---|---|
| 事务与事实写入入口 | `core/wallet.py:59,89` 行锁/流水，`core/assignment.py` 绑定与一致更新 | 以现有数据库工厂建立 UoW；统一执行身份、guard锁序、FactSet revision与命令提交 | 相关写同锁序，初次快照一致、提交前重验，真实数据库竞争与回滚 |
| 持久幂等 | 钱包/用量的业务唯一键与重复处理 | CommandExecution稳定幂等范围、输入摘要、并发竞争、attempt与逻辑动作分离 | 同key同输入原结果；不同输入冲突；提交回执丢失可查询，不能盲重放 |
| 事务审计 | `core/audit.py:59` 稳定动作、before/after、操作者快照，同Session写入 | 扩展actor/effective主体、任职/委托、批准/manifest、decision和execution引用；单独控制读者和敏感字段 | 结果与必要审计同事务，回滚不留假成功；日志和审计不包含凭据/无关个人资料 |
| outbox与对账 | `assignment.write_outbox` 与业务写同事务，`gateway/snapshot.py:51` 消费记录 | Stage持久投递租约、DeliveryAttempt、稳定接收端去重键、失败重试、pending/unknown与回执查询 | 重复消费、worker崩溃、已接收但回执丢失，不丢业务意图或虚报投递成功 |
| 运行资源与后台任务 | `gateway/app.py:67` 启停、writer drain、客户端/Redis/DB清理；scheduler任务异常边界 | 把可借鉴行为装进ComponentContext/EffectScope和provider租约；明确取消、排空与失败状态 | 不遗留任务/子进程；停接入后等旧代次收敛；清理失败不能报告成功 |
| 可观测性与错误归因 | trace、指标、上游/代理/内部错误分类、JobResult | Stage按认证、授权、准入、WASM、事务、投递阶段定义错误和关联ID | 验签失败、无权、超限、未提交和结果未知可区分；诊断自身不越权 |
| 工程门禁与故障场景 | 契约检查、状态枚举、并发/去重/回滚与租户测试 | 延续已有工具；按Core/I1/W1/O1/F1改写测试输入与断言，验证迁移和部署目标 | 在Stage重新运行且保留证据；静态检查、SQLite、真库和生产运行结果分开 |

不把这些内容全部放进Runtime Core：Core管理运行资源，execution/storage负责命令与持久化，业务领域解释业务不变量。后台worker由Core监督，审计事实不因组件卸载而被删除。

## 3. 需要重新设计的部分

- **身份入口**：保留“统一构造可信上下文、权限从当前权威记录读取”的经验。Z Pool本地密码/JWT、全局角色与成员权限并集不直接成为Stage身份模型；Stage按选定真实协议profile、ExternalIdentity、任职/ServiceGrant/有限委托实现。
- **能力注册**：借鉴显式注册、重复key拒绝、Protocol和未知能力不静默降级；Stage追加不可变CapabilitySpec、精确实现摘要、generation和provider lease。注册表不是完整生命周期引擎。
- **并发与配额**：Redis原子占槽是专用网关机制。Stage 使用 PostgreSQL 权威 guard、幂等、outbox 和业务租约，以及有限 runner 队列/进程预算；Redis 8 的可重建缓存、短期提示与唤醒按实际需求逐项接入。每项明确 tenant/权限版本、超时和故障策略，Redis 清空不丢业务事实，不复制账号资源池的 key/额度语义，也不以 Redis 锁取代 PostgreSQL 业务不变量。
- **调度与恢复**：借鉴任务异常边界和显式资源收尾。Z Pool单副本APScheduler不是Stage通用持久流程协议；当前不为复用而强制新加调度框架或三进程部署。
- **响应和错误**：保留稳定错误码、请求关联和脱敏原则；不照搬Z Pool控制面的R/P包装及业务错误HTTP200策略，Stage保留自身API契约。

## 4. 暂不迁入的业务

供应商、渠道、代理库存、模型网关、模型价格、计费、钱包、结算、提现、调账、资源配额等属于Z Pool领域。Stage当前P1不需要这些实体和接口，不能以补齐旧项目规模为目标。未来确有相关业务需求时，应作为有独立边界的业务能力重新评审。

## 5. 复用前必须承认的实现限制

1. `wallet.move` 有集中原语，但 `settlement._credit_wallet` 仍存在独立余额更新路径。不能先假设全仓共享同一行锁/幂等协议。
2. Z Pool封禁/绑定/日志/outbox的数据库事务与Redis重置不构成跨存储原子提交。
3. `routing_outbox` 的通用消费函数主要把指定事件标为processed并推进路由版本；资源选择仍查询DB。不能把它当成已实现任意外部接收端的可靠投递。
4. 每请求重新加载用户/成员状态有价值，但不等于会话精确撤销、OIDC完整验证或Stage事务内撤销竞争协议。
5. 原有进程内队列、单副本scheduler和清理代码都需按Stage重启/取消/版本更换要求复核。原项目测试不自动覆盖新契约。

这些是选择复用方式的依据，不是本轮对原项目生产故障的结论。本轮没有运行其服务或测试。

## 6. 实施顺序

1. **P1a先固定共同接口和责任**：CapabilitySpec、ExecutionContext、CommandExecution、UoW、审计/outbox信封与Core资源owner。已有Stage基础继续使用，Z Pool实现作为具体参照。
2. **随P1b接入真实身份与可信写入**：统一loader、明确任职/服务授权与guard协议；幂等和审计持久化围绕真实命令身份建立。
3. **随P1c接入独立runner与准入**：WASM、签名和密码验证使用已评审的适配及成熟底层实现；这些能力不能从Z Pool复制出来。
4. **P1d贯通持久结果与恢复**：EvaluateQualification的输入一致采样、无锁计算、最终重验、结果/审计/outbox提交，加上测试sink的重复与unknown对账。

每一项迁入都必须回答：服务于哪个P1场景、归属哪个Stage契约、是否替换已有功能、带入哪些依赖、原行为有哪些已知限制、目标环境怎样验证。不能通过复制整套core或改几个实体名完成验收。

本轮已按计划落地命令/幂等/审计/outbox/delivery 第一版；尚未接入真实身份、WASM、HTTP 或生产外部 sink。下一步仍按P1接口冻结与真实环境门禁推进。
