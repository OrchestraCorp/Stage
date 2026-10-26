---
title: Stage 授权与业务并发优化实施复查
status: implemented-and-reviewed
date: 2026-10-26
policy_effect: current-code-boundaries
---

# Stage 授权与业务并发优化实施复查

本轮落实[最终优化计划](../stage-concurrency-improvement-plan.md)，以原冻结版本 `0560abe` 为基线。范围是当前人员业务、身份和原生认证的事务协调；HTTP 仍为25个操作、应用表仍为37张，Alembic head 仍为 `c6f3a19d820b`。WASM、持久流程、审批与多节点能力没有被提前实施。

## 1. 已实施的改动

| 项目 | 实际变化 | 保留的保证 |
|---|---|---|
| 身份验证去重 | AuthenticatedAuthorizationLoader 只加载绑定的 facts；kernel 每阶段执行一次完整身份校验 | 版本冲突／允许决定前先验身份；reader后、handler flush后仍完整重验 |
| 组织协调 | [coordination](../../app/storage/coordination.py)提供显式 SHARED/EXCLUSIVE；普通业务与身份使用S，管理授权写者使用X | trust/principal锁序、集合新增／缺失事实保护、撤权提交顺序 |
| 幂等协调 | 短命令及原键查询使用同一事务级 advisory 键，先于 trust/principal/org | 原完整唯一元组、参数冲突、原键等待／恢复、unknown、数据库唯一约束 |
| 业务对象 | 查询／重放读取 WorkItem S；实际编辑 WorkItem X | 当前授权、expected_version、领域状态检查、单事务审计 |
| 原生账号 | `read_account()`用于UserInfo与本人session/consent列表；实际修改仍`lock_account()` | 查询principal S/account S；修改principal X/account X；组织S；批量client变化仍trust X |
| 事务所有权 | 精确根事务及连接事务证明；禁止模式升级、错组织、跨事务和savepoint；kernel前后检查证明 | 异常即使被处理器捕获，失效证明也不能提交／披露；取消清理完成后才释放资源 |
| 嵌套命令 | kernel操作上下文拒绝同调用树内嵌套操作，包括handler创建的子任务 | 防止新Session等待调用方自己持有的幂等／对象锁；异常／取消后重置上下文 |
| 旧接口 | 生产、脚本、测试均移除`lock_tenant()`，AST门禁禁止回归 | 所有组织保护明确声明模式，保留X的路径不再依靠隐式默认 |

正式执行顺序：短命令键mutex → trust S → principal S → org S → work X/S。纯查询省略键mutex。管理操作按其写者范围保留X；成员／角色／任职／ACL／scope／resource／MFA变更与日常操作仍正确冲突。

幂等锁规范编码为紧凑JSON数组 `['stage.command-key',1,org,actor,name,version,key]` 的UTF-8 bytes，不做Unicode归一化；SHA-256前8字节分为两个有符号32位整数，使用PostgreSQL二整数事务advisory命名空间。它与现有控制面bigint advisory命名空间分离。完整元组的SQL唯一约束仍是最终防线，摘要碰撞只会增加等待。锁键不包含目标、payload、任职或request-id。

## 2. 复查发现及修复

第一批验证发现，身份失效时若先依据缺失facts返回授权拒绝，会改变原来的认证／身份错误语义。完整身份校验现放在facts加载之后、授权决定及版本冲突披露之前，保留原路径语义；事实与请求的对应关系仍在决定前核对。

独立架构复查发现一个新协调协议的误用窗口：savepoint被拒绝后，handler可能捕获异常，而最终提交尚未消费“证明失效”的状态。已在kernel授权前后与最终提交前统一断言保护有效，新增handler／reader吞异常、Connection和ORM保存点回归证明不能提交或披露。

恢复入口另补充了等锁后及查行后的deadline检查，避免显式期限已过却被误报为结果不存在；两个真实PG竞争回归通过。

旧loader单测原来依靠loader隐式取得组织X；新协议要求调用方先声明模式。已将这些测试迁至明确入口，保留原授权断言，没有通过放宽生产检查解决测试失败。整库首轮的这6个测试失败已在定向PG/SQLite复测修正。

两轮源码复核覆盖所有当前身份、原生凭据／会话、管理写者及kernel调用。未发现剩余需要阻止本轮交付的授权或锁序问题。审查不是外部安全认证，未来新增writer仍须进入同一协议清单。

正式含写者负载进一步发现：仅使用组织共享行锁时，MFA写者最大等待约5.4秒。独立PostgreSQL控制实验确认，后发共享行锁读者可超越已经排队的排他写者，持续读流会推迟撤权事务取得保护。本轮据此增加**同一组织的事务级advisory共享／排他准入门，再取得原guard行锁**；没有自行编写公平队列，也没有替换原行锁安全边界。后续容量与全部回归以该组合为准，行锁候选数据保留为对照。

独立对照原始记录：[共享行锁与advisory](stage-coordination-fairness-2026-10-26.json)、[advisory门＋行锁](stage-coordination-fairness-hybrid-2026-10-26.json)。在8／16个持续读者、2秒读流中，行锁写者约等至2秒末尾且被数百／千次后发读取超越；组合协议写者约40～66ms进入，后发超越为0。这是锁准入控制实验，不是Stage吞吐数据。

组织准入门使用bigint高32位ASCII `STGO`，低32位来自组织规范引用摘要；与现有控制面bigint键的高位及命令二integer命名空间分别隔离。Stage仍只有一个授权根；组织摘要碰撞最多增加等待，guard行与固定组织检查仍在。

该门只处理已证实的组织入口排队问题，不宣称任何调度或部署条件下都有固定撤权时限；principal/trust的局部行锁与其既有写者范围继续保留，真实部署SLO另验。

## 3. 真实并发与回归证据

新增测试采用明确事件与PostgreSQL阻塞边，避免以“运行得比较快”代替并行证明：

- [协调生命周期](../../tests/storage/test_coordination.py)：共享重叠、写者冲突、同键与异键、advisory命名空间、取消、根事务重用和保存点。
- [业务并发](../../tests/execution/test_concurrent_operations.py)：不同对象命令、同对象纯读、同键重放／目标和payload冲突、原键及UUID恢复、回滚、取消、同对象版本竞争、等键时撤权及五种授权写者双向排序。
- [原生读取与写入](../../tests/native_auth/test_read_coordination.py)：同人UserInfo/session/consent读取重叠、不同账号refresh并行、revoke_all与读的双向排序、client批量失效与两账号读取协调。
- [操作边界](../../tests/execution/test_operation_boundaries.py)：捕获保存点异常、子任务嵌套、取消／异常后上下文清理及独立请求隔离。
- 既有身份／MFA／凭据到期、同事务误改身份、撤权竞争、审计／outbox原子性与unknown回归继续执行。

第一批独立PG协调／业务测试32通过、1个SQLite专用跳过；扩展PG组77通过、1跳过。数字是各次运行范围，不相加当成全部测试数。

<!-- final-validation:start -->
| 检查 | 最终结果 |
|---|---|
| PostgreSQL＋Redis 全量 | **1547通过／10跳过／0失败**；[逐例摘要](concurrency-postgresql-tests-2026-10-26.xml) |
| SQLite 开发快测全量 | **1454通过／95跳过／0失败**；[逐例摘要](concurrency-sqlite-tests-2026-10-26.xml) |
| 真实 Chromium 两场景 | **2通过／0跳过／0失败**；[逐例摘要](concurrency-chromium-tests-2026-10-26.xml) |
| 开发设备重启后 | 5项PG/Redis/HTTP探针、9项原生认证、6项浏览器登录通过 |
| 静态／合同 | Ruff、263文件格式、259文件Mypy、OpenAPI合同通过 |
| Runtime 映射 | 最终PG JUnit映射46/46 |

完整检查期间应用、测试和验收脚本摘要保持不变；[机器记录](concurrency-validation-2026-10-26.json)保存输入摘要和逐项结果。各定向组与全量可能重叠，不能相加。Authlib互操作测试客户端的两条废弃提示保留，未影响验证。
<!-- final-validation:end -->

## 4. SQL与容量

原基线的107／85条SQL来自同一容量fixture。去重并采用共享行锁的候选为读取79条、编辑72条；读取比初始静态预计81再少2条，因为loader不再重复取得组织guard。最终增加组织准入门后，每个业务请求的前置认证和提交事务各增加一条advisory语句，正式实测读取81条、编辑74条。

行锁候选UserInfo为10条、refresh为16条；最终组织准入门分别增加一条语句。scope层级和SSO browser session会改变查询数量，不以这些fixture数字约束所有请求，也不牺牲最终身份检查追求计数。

测量器已识别FOR SHARE、FOR UPDATE和事务advisory锁，分开记录organization、principal、trust、work_item、account/session及command_key/control administration的获取／持有区间，并增加p99。同事务同一资源重取不重复累计持有时间。获取耗时包含SQL/驱动往返，持有区间包含事务清理／连接归还，均不是PostgreSQL内部精确等待时钟。

追加负载包括：真实版本化MFA策略写者与人员操作混合、同人并行UserInfo、16个相同幂等键编辑重试。策略在前置认证与业务提交之间变化时，旧evidence被拒绝属于预期安全结果，单独报告而不隐式重试或伪装全部成功。

<!-- capacity-results:start -->
正式数据：[最终组合协议](concurrency-capacity-2026-10-26.json)、[未采用的共享行锁候选](concurrency-capacity-row-candidate-2026-10-26.json)、[原冻结基线](p2p3-capacity-2026-10-26.json)。每档512次、16个独立人员/对象、单进程与相同连接池配置，分别在无其他测试进程的窗口采集。不是同一时刻的A/B实验，宿主机背景负载没有完全控制。

| 并发 | 原吞吐/s | 最终吞吐/s | 原p95 ms | 最终p95 ms | 最终p99 ms | 最终组织门＋行锁获取p95 ms |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 30.402 | 34.222 | 63.555 | 51.177 | 55.542 | 0.805 |
| 4 | 33.081 | 41.314 | 210.391 | 172.723 | 180.340 | 6.633 |
| 8 | 32.408 | 40.209 | 405.918 | 352.976 | 436.625 | 13.671 |
| 16 | 31.505 | 40.007 | 830.043 | 743.519 | 941.204 | 30.126 |

2048次基线混合操作和16人真实密码＋TOTP登录全部成功。额外256次含策略写者流量中，28次因当前MFA策略版本改变而拒绝旧evidence，属于预期安全结果；8次管理策略变更全部成功。所有场景累计555条命令、审计和工作项版本增量一一对应。

策略写者p95 358.687ms、最大369.431ms；行锁候选最大5435.603ms。16个同键重试仅产生1次提交、15次重放；同一人员128次并行UserInfo读取全部成功。

最终SQL实测：UserInfo 11、refresh 17、工作项读取81、编辑74。相较共享行锁候选79/72，多出的组织准入语句用于阻止等待写者被后发读者持续超越；没有为追求更高吞吐保留该问题。

普通混合负载吞吐仍未随并发线性增加；单进程调度、SQL往返和对象/主体局部保护仍有成本。本轮不承诺生产SLO，也未把后续批量查询、跨请求缓存或多节点调度当成已经优化。

行锁候选记录保留原始观测，其早期分类器可能把account语句中的principal_ref误归principal，并把guard epoch UPDATE重复归入资源锁分布；最终分类器按FROM/JOIN/UPDATE表名识别，PG只统计显式锁语句，组织门、行锁与管理锁分别记录。候选吞吐、总延迟和首次guard等待未受该分类错误影响；候选的细分类锁分布不用于最终比较。
<!-- capacity-results:end -->

## 5. 保留边界与技术路线

组织写者仍有明确广域X，账号修改仍principal X，client配置仍trust X。它们是经过评审的保护范围，并不意味着所有写操作已经无等待。

内部 `get_result(execution_id)` 保留显式org X；默认HTTP恢复使用原键同步。Outbox尚未默认挂载，领取／回执／恢复继续显式org X。两项的模块责任与新消费者接入前退出条件见[过渡清单](../stage-concurrency-improvement-plan.md#transition-exits)；旧隐式helper本轮已经移除。

事务advisory锁不代表持久attempt所有权，不能跨WASM、网络或人工等待。P1d的持久受理与条件终态、资格FactSet集合revision、批准／激活writer必须按各自合同接入。新增任务创建或审批若改变权限关系，不能直接使用普通元数据命令的S模式。协调模块留在storage/execution，不进入纯Runtime，也不开放给WASM或蓝图自行配置。

当前单worker升级按停接入、排空、切换确切制品、恢复就绪执行。新旧org X/S仍共享同一guard行，但这不是未来任意锁键编码／后端混合升级的证明。制品冻结、迁移head和验证输入摘要按既有交付流程留证。
