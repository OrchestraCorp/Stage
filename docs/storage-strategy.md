---
title: Stage PostgreSQL 与 Redis 存储策略
status: accepted-decision
decision_date: 2026-10-25
policy_effect: engineering-only
---

# Stage PostgreSQL 与 Redis 存储策略

部署约束：每套 Stage 使用一个组织授权根的专属 PostgreSQL 数据库和一个环境；根是信任与治理边界，不等同于一个法人公司。部门/项目用 scope，集团/子公司等组织单元以后按需建模。保留的 `tenant_*` 存储名指唯一部署组织根，不提供共享数据库 SaaS 多租户部署。数据库根单例约束和 bootstrap 环境检查共同防止误接另一根/环境；历史多根数据需人工梳理，迁移拒绝自动合并。详见[组织授权根语义](organization-root-semantics.md)。

2026-10-25 用户确定：**PostgreSQL＋Redis 作为 Stage 的长期存储组合。** 本轮工程基线采用 PostgreSQL 17 作为唯一主数据库、Redis 8 作为辅助存储，不再维持 MySQL 生产适配或双库兼容要求。SQLite 保留为快速开发测试工具，不能限制 PostgreSQL 的领域建模与事务能力，也不能代替 PostgreSQL 的真实验收。

这是工程选型决定，不表示 Runtime Core、真实 IAM、WASM 或完整业务切片已经实现。当前代码与测试结果按提交和环境单独记录；历史双库评审、迁移代码与验证 JSON 保留原貌，不改写成新策略的验收证据。

当前短命令协调已明确组织S/X与事务级逻辑幂等键，见[并发优化](reviews/concurrency-optimization-2026-10-26.md)。advisory键只保护同一PostgreSQL事务，不是持久任务租约；Redis仍不参与权威提交。

## 1. 事实权威与提交边界

| 存储 | 负责内容 | 恢复与一致性要求 |
|---|---|---|
| PostgreSQL 17 | Stage 权威主体/组织关系、授权事实与撤销版本、批准/激活、业务对象、幂等结果、审计、outbox、投递 attempt、业务租约及 fencing 状态；尚未实现的模型按阶段建设 | 业务结果、命令终态、必要审计与 outbox 同一 PostgreSQL 事务提交；提交回执未知时按原执行身份对账 |
| Redis 8 | 可重建缓存、非权威短期协调、可丢失唤醒；每项用途独立定义 TTL、容量、超时与故障策略 | 清空、重启、淘汰或短时不可用不能丢业务事实，不能扩大授权或重复提交副作用 |
| SQLite | 单机快速功能、类型与迁移检查的适用子集 | 不作为生产配置，不证明行锁、并发撤销、约束或 PostgreSQL 迁移正确性 |

SQLAlchemy async、Alembic 与每命令独立 UoW 继续使用，生产驱动为 `postgresql+asyncpg`。可按业务需要采用 PostgreSQL 的约束、索引和事务能力，不再以兼容 MySQL 为设计前提。新增 PostgreSQL 专属机制须有迁移及真库测试；不要求 SQLite 模拟全部生产语义。

当前授权 guard、命令幂等、outbox/DeliveryAttempt 和业务执行权的权威租约留在 PostgreSQL。Redis 锁、TTL 或计数器不能替代这些记录与 fencing；Redis key 到期也不能证明旧执行者已经停止。

PostgreSQL 和 Redis 之间不承诺双写原子性。先提交 PostgreSQL，再发失效通知或唤醒；允许通知遗漏、重复和延迟，由当前版本核验、周期扫描或显式重建恢复。若辅助写入失败，不能把已确认的业务提交报告为回滚；原事务结果未知时仍查询 PostgreSQL 的原 CommandExecution。

## 2. Redis 使用约束

每个接入点在实现前写明以下行为，而不是只声明“使用 Redis”：

- **缓存**：命名空间至少区分部署环境、组织、对象/投影及 schema；受权结果还要区分主体、有效主体、选定任职/委托、action、策略与授权版本。读取仍验证当前身份、撤销状态和读权，不能复用旧 `allowed=true`。命中、失效与重建均不得旁路部署组织约束与主体/scope 授权。
- **撤权与失效**：权威版本在 PostgreSQL；仅靠 Redis pub/sub 清缓存不能提供撤权保证。安全相关路径必须取得足够新鲜的权威状态，无法确认时拒绝或返回不可用。普通缓存故障可以回源，但需有界并发和防击穿策略。
- **短期协调**：只用于丢失后不会破坏领域不变量的提示、节流或去抖。涉及安全或容量上限的机制须明确 Redis 故障时拒绝还是采用已验证的保守回退；不能自动放行，也不把辅助锁写成业务唯一执行保证。
- **唤醒**：Redis 消息只提示“应检查 PostgreSQL”。outbox worker 仍从 PostgreSQL 领取并记录租约/attempt；订阅断线、消息丢失和 Redis 清空后靠扫描恢复，接收端继续按稳定 ID 去重。
- **资源与秘密**：设置有限连接/连接池、命令超时、key TTL、payload 大小和队列/内存预算，约定淘汰与重连行为；URL 和凭据脱敏。不把用户 bearer token、refresh token 或签名秘密作为通用业务缓存。

Redis client factory 仅提供可显式装配的基础能力。本轮不把 Redis 注册为 `main` 的强制启动或 `/health` 依赖；未来组件按实际用途声明依赖与降级策略。某能力必须依赖 Redis 时只阻断相应入口，不能静默绕过安全控制，也不能误报整个持久业务已经丢失。

## 3. 本地环境与部署边界

本轮约定根目录 `compose.yaml` 提供 PostgreSQL 17 和 Redis 8，本地端口只绑定回环地址 `127.0.0.1:15432` 与 `127.0.0.1:16379`，避免占用其他项目的默认端口。`.env.example` 提供虚构开发配置，实际 `.env` 和凭据不入库。开发 compose 的密码、端口与存储布局不构成生产部署方案。

开发 PostgreSQL 数据库/用户均为 `stage`，虚构密码为 `stage-dev-postgres`；开发 Redis 虚构密码为 `stage-dev-redis`，关闭持久化，`maxmemory=256mb`、`maxmemory-policy=noeviction`。该策略下内存耗尽会拒绝部分写操作，使用方必须处理错误；配置不是无限容量或业务可靠性的保证。

生产使用 PostgreSQL 17 并显式提供数据库 URL；Redis 8 的访问控制、网络边界、容量、可用性与监控按接入用途配置。即使 Redis 启用持久化，也不能据此成为业务事实的唯一保存位置。PostgreSQL 的备份、恢复与迁移演练独立验收，Redis 缓存恢复不替代主库恢复。

## 4. 迁移与验证

MySQL 退出受支持配置和后续生产门禁。既有 Alembic revision 及其中的 MySQL 方言分支作为历史迁移保留，不为这次选型重写；它们的存在不表示当前应用继续支持 MySQL。若存在真实旧库，跨引擎数据搬迁必须单独盘点与验证，本决定不自动执行迁移、删除或重建用户数据。

后续新增模型以 PostgreSQL 17 的空库升级、前向升级、模型漂移、时间/标识符语义、约束与并发作为正式门禁。用真实连接和屏障验证 guard 锁序、撤销与提交竞争、幂等冲突、租约恢复、提交未知和 outbox 对账。SQLite 快测与 DDL 编译结果分别报告。

Redis 接入增加不可用、超时、清空/淘汰、重连、失效消息丢失与部署环境、主体/scope 缓存隔离测试；必须观察业务事实仍在 PostgreSQL、旧权限不生效、遗漏唤醒可恢复。每个使用方自行证明故障策略，client factory 测试不能替代业务接入验收。

仓库来源为 GitHub，主 CI 使用 GitHub Actions，GitLab CI 保留为镜像环境模板；两者使用 PostgreSQL 17 与 Redis 8 检查，不再要求 MySQL。Redis 探针使用显式 `TEST_REDIS_URL`，仅操作随机测试 key 并验证 PING、NX 和 TTL，不执行 FLUSH。CI 定义与本地成功均不表示远端流水线已经运行。

本轮已通过 SQLite 环境 228 项、PostgreSQL 17.11 环境 236 项测试，PG 迁移循环及 Redis 8.2.3 连接/NX/TTL 探针也已通过，详见[存储切换验证](reviews/storage-switch-validation-2026-10-25.md)。未执行的场景不计通过，P1 的 Core/I1/W1/O1/F1 场景及生产运维验收仍保持原范围和证据要求。
