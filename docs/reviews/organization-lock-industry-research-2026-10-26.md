---
title: 组织锁与授权并发——成熟方案调研
status: research-only-not-an-approved-implementation-spec
date: 2026-10-26
policy_effect: none
---

# 组织锁与授权并发——成熟方案调研

用户要求先研究成熟做法，不盲目编码。本轮读取 PostgreSQL 17 官方文档、Google Zanzibar 原论文、SpiceDB/OpenFGA/OPA 官方文档及 SQLAlchemy 2.0 文档，并对照 Stage `0560abef3872ad40d406ad4e8a54b3ad35f800e5`。**未修改应用、测试、数据库、依赖或锁协议；下文是候选方案与研究判断，不是已批准实施规格。**

后续补充：[成熟 OA 与企业应用实际源码对照](oa-identity-permission-concurrency-research-2026-10-26.md)，核查组织身份、任务资格、单据版本和流程实例内串行，仍不修改实现。

## 1. 结论与前一轮建议的修正

只把全组织排他锁改成全组织共享行锁，可以解除普通请求之间的大部分互斥，但还不是完整的优化设计。成熟机制提供四个可组合的方向：

1. **先定义一致性合同。** 权限查询足够新、业务对象不丢更新、撤权和业务提交有顺序，是三件不同的事。
2. **数据库内缩小冲突范围。** 授权读者共享保护，授权写者排他保护；业务对象版本、幂等键和一次性凭据分别协调。组织、人员、资源的保护范围按实际依赖逐步细化。
3. **缩短事务、复用有版本依据的事实。** 一次加载已锁定事实，避免同一事务反复查询；未来耗时的纯计算在事务外，最终提交使用短事务重验。授权时间与当前撤销仍由服务端核验。
4. **根据规模选择快照/缓存/外部授权系统。** Zanzibar 系统的强项是大量关系检查及其因果新鲜度，并不自动提供 Stage 业务库的原子提交屏障。

对当前 Stage，更值得验证的是“同一 PostgreSQL 内的短事务＋适当范围的共享/排他保护＋对象版本与幂等协调”。共享行锁与**事务级 advisory 锁**都属于成熟候选，需要同口径测量。现在没有证据支持直接引入分布式授权服务，或宣称某种锁一定能提高多少倍。

## 2. 必须区分的三种一致性

| 要求 | 具体含义 | 不能据此推导的保证 |
|---|---|---|
| 授权查询新鲜度 | Check 能看到指定版本或此前已完成的权限修改 | Check 返回以后，到另一个数据库 COMMIT 之间不会再撤权 |
| 业务对象并发正确性 | 两人同时修改一个对象，不覆盖未读到的版本；同幂等键不重复执行 | 任职、MFA、账号或会话在另一张表变动时自动使该写入失败 |
| Stage 当前撤权提交合同 | 依赖旧授权的业务若先进入受保护区，撤权等待它结束；撤权若先提交，后续业务不得按旧授权提交 | 管理员刚点击撤权就取消所有在途请求；或者已发到网络的响应可以收回 |

当前 guard 同时承担了上述多个职责，因而出现全组织互斥。优化应拆清职责，不应悄悄改变第三项合同。若产品将来允许“在权限检查时获准的在途操作可在撤权后完成”，可采用更宽松设计，但这是产品语义决定，不能伪装成透明性能优化。

## 3. PostgreSQL 官方的做法

### 3.1 MVCC、SSI 与锁解决不同问题

PostgreSQL 17 的 Read Committed 每条语句取得新的快照；Serializable 使用 SSI 检测危险的读写依赖，必要时中止事务并由应用重试。[P1][P2]

官方对依赖顺序有一句直接说明：

> “The reader then appears to have executed first regardless of which started first or which committed first.” [P2]

因此，业务先读旧权限、撤权随后提交、业务再写另一条业务记录并提交，可能仍存在合法的等价串行顺序“业务→撤权”。只有单向依赖时不必产生 SSI 要中止的环。**仅切换 SERIALIZABLE，不自动满足 Stage 当前的撤权提交顺序。** 这不是 SSI 不可靠，而是所需合同与其单独提供的保证不同。

普通的 epoch 前后两次读取、在业务 UPDATE 的 WHERE 中加权限子查询，也存在类似边界：最后一次读取或语句快照之后，授权写者仍可能提交。对象版本 CAS 只协调该对象，不能自动覆盖其他表中的权限事实。

### 3.2 RLS 也必须处理跨表权限竞态

PostgreSQL 的 RLS 文档直接给出“旧权限子查询＋新敏感数据”的泄露案例，并明确警告：

> “such accesses can create race conditions that could allow information leakage” [P3]

官方列出的处理思路包括：权限子查询加 `FOR SHARE`，权限更新期间采取排他保护，或在权限更新后等待旧并发事务结束。RLS 可作为数据库执行边界，但不是把 RBAC/ReBAC/ABAC 搬进 SQL 就自动解决所有竞态；当前也没有必要为性能问题重写整个授权引擎。

这也限制了 SQL 合并方法：不能随意把“等待锁＋读取多张权限表”塞进同一复杂语句，就假设所有子查询都能看到等待后最新状态。应按明确顺序取得保护，再用 Read Committed 的新语句读取相关事实。[P2][P3]

### 3.3 共享行锁与事务级 advisory 锁

| 候选 | 成熟机制 | 优点 | 对 Stage 的约束 |
|---|---|---|---|
| 授权 guard 行 `FOR SHARE`；写者 `FOR UPDATE` | PostgreSQL 行锁兼容矩阵 | 与当前实现接近；普通读者可并行；事务结束释放 | 大量事务共享同一行会有 tuple/MultiXact 管理开销；授权写者仍等待相关读者 |
| `pg_advisory_xact_lock_shared` / `pg_advisory_xact_lock` | PostgreSQL 事务级 advisory 锁 | 可按稳定的组织/人员/资源标识表达逻辑冲突；无需锁一个实体 tuple | 数据库不强制任意 SQL 遵守该协议；锁键映射、命名空间、容量、顺序与所有写路径必须完整 |
| 对实际权限行加共享锁 | PostgreSQL 普通行锁 | 冲突范围可能更小，实际 UPDATE 自然冲突 | 只锁现存行不能保护“关系不存在”及新插入的 deny/ACL；权限继承和依赖集合更难完整覆盖 |

官方说明 advisory 锁可避免用于同类应用锁方案的表膨胀，并提供较低开销；这不构成 Stage 场景一定更快的实测结论。[P4][P5] 多事务锁同一行会涉及 MultiXact，需观察共享行锁方案的实际开销。[P6]

这里讨论的是 **transaction-level** advisory 锁，事务结束自动释放；不建议在连接池中采用 session-level 锁，以免事务回滚后锁仍留在复用连接上。继续使用全组织排他 advisory 锁，仍然会把普通操作串行化。

`FOR KEY SHARE` 也不能随意当作更便宜的权限读锁：它不阻止普通非键字段的 `FOR NO KEY UPDATE`。如果依靠独立 guard 协议，必须确认所有授权写者都显式拿同一 guard 的 `FOR UPDATE`，不能认为 KEY SHARE 天然保护了 status/version 等字段。[P4]

官方建议同一对象第一次取得的锁就是本事务所需的最强模式，且多对象遵守统一顺序。[P4] 两个共享持有者同时升级排他可能死锁。Stage 内部 loader 当前会再次取得排他 guard，所以模式需要贯穿所有调用，不能只修改外层 helper。

## 4. Zanzibar、SpiceDB、OpenFGA 实际优化了什么

### 4.1 Google Zanzibar：因果快照＋版本绑定缓存

2019 年原论文 §2.2/§2.4.4 使用 Spanner 时间戳和一致快照，客户端把 zookie 与内容版本一同保存；后续检查不能使用比该内容要求更旧的权限快照。它解决撤权后新内容仍被旧 ACL 放行的 “new enemy” 问题。[Z1]

论文 §3.2.5 的扩展方法包括：按对象分发、分布式缓存、中间检查结果复用、把快照时间戳放入缓存键，以及同键请求合并。它依赖的是**缓存结果属于哪个权限版本**的明确证据，而不只是设置一个 TTL。

论文还明确说 content-change check：

> “does not need to be evaluated in the same transaction as the application content modification” [Z1]

所以，Zanzibar 的因果新鲜度协议不能直接解读成“所有外部业务提交与撤权由同一个事务排序”。它的保证应按论文定义和客户端协议理解，不能将两个不同的提交合同混为一谈。

### 4.2 SpiceDB：请求可选新鲜度与 ZedToken

SpiceDB 官方文档提供 `minimize_latency`、`at_least_as_fresh`、`at_exact_snapshot`、`fully_consistent`，用 ZedToken 表示数据版本下界。[S1][S2]

其中 fully_consistent 明确限定：

> “the snapshot used will be loaded at the beginning of the API call” [S1]

这能回答 Check 的新鲜度，不能替 Stage 持有业务提交锁。ZedToken 适合随资源版本保存、随后检查传回；它不是给任意后续业务写入的授权票据，也不能让不可信客户端自由选择过旧版本来绕过服务端要求。

SpiceDB 官方关系写入说明列出应用库事务＋WriteRelationships＋失败补偿，以及流式同步等方式，并明确应用负责维护关系数据。[S3] 其中的 “2 phase commit-like” 示例不是两个数据库真正的原子两阶段提交，补偿仍可能失败；不要因接入产品就忽略双写、撤销传播和崩溃恢复。

当前文档还指出某些数据存储和模式有额外一致性限制，例如 CockroachDB 下 fully_consistent 的跨节点时间戳边界；应遵循对应版本/数据存储文档。不能只看模式名称推导保证。[S1]

### 4.3 OpenFGA：缓存优先与跳过缓存

官方 Query Consistency Modes 表明确：`MINIMIZE_LATENCY` 在可用时用缓存，`HIGHER_CONSISTENCY` 跳过缓存直接查询数据库。[F1] 同页说明缓存默认关闭；不能误称 OpenFGA 默认一定返回旧缓存。

这是一种明确的查询新鲜度与性能取舍，也不把 Check 调用和 Stage 的业务 COMMIT 合并成一个事务。关系图复杂、多个业务服务共享授权事实时，它比本地手写遍历更有吸引力；当前单任职、直接 ACL、同库 ABAC 的 Stage 是否需要它，应由关系规模和多服务需求证明。

### 4.4 OPA：策略计算与事实来源分离

OPA 文档说明它持有数据/策略的副本或缓存，并非事实源；外部数据注入、bundle、拉取等方式有不同新鲜度。[O1] 因此策略引擎或未来 WASM 可以优化/隔离规则计算，但无法代替 Stage 对当前身份、版本、撤销和提交的一致性负责。

## 5. 对象乐观并发值得复用，但不替代授权同步

SQLAlchemy 2.0 的标准 version counter 会生成类似 `UPDATE ... WHERE id = :id AND version_id = :previous_version` 的条件更新，匹配零行时识别旧版本。[Q1]

这适合减少业务对象更新对大范围锁的依赖；Stage 已有 expected_version，可以研究以数据库条件更新或明确对象写锁落实，而不需要发明新的对象并发理论。但仍要独立保留：

- 同一个幂等键的串行或唯一约束冲突后重读协议；普通重复提交不能退化成版本冲突。
- 一次性 code、refresh、OTP 的消费与撤销协议；它们不能复用返回旧秘密的业务幂等行为。
- 当前权限的保护与重验；业务对象版本没有包含人员、角色、会话和组织策略的所有变化。
- 审计和结果与业务修改同事务、提交未知按原键恢复。

## 6. Stage 候选方案比较

| 方向 | 解决的问题 | 实现/运维代价 | 当前研究判断 |
|---|---|---|---|
| 合并重复读取、锁内事实一次加载、短事务 | 降低 SQL 往返与持锁时间 | 低到中；需要证明复用边界 | 应最先设计，收益可与现有 107/85 SQL 基线比较 |
| 组织共享/写者排他，局部对象/幂等同步 | 解除无关普通操作的互斥 | 中；需完整冲突矩阵与并发回归 | 最小可信迁移候选；比较行锁和事务 advisory 锁 |
| 人员/资源/组织分层保护 | 局部撤权不阻塞全组织 | 中到高；依赖覆盖、空集合、锁顺序复杂 | 根据写者频率与实测再引入，不第一步全面铺开 |
| 版本化权限事实缓存＋最终保护 | 减少跨请求重复事实加载和计算 | 中到高；失效、依赖版本和时间需完整 | 先缓存可证明不可变的发布内容；不直接缓存“允许”结果 |
| PostgreSQL SSI | 通用多行事务异常检测 | 重试、读写路径一致使用的成本 | 可用于合适业务；不单独替代当前撤权提交屏障 |
| SpiceDB / OpenFGA | 大量关系图检查、共享授权服务、版本化缓存 | 新服务、数据同步与跨库一致性 | 当前没有充分理由直接引入；未来多服务/复杂关系再评估 |

分层保护可先只表达：组织策略读/写、人员资格读/写、资源授权读/写。普通操作按依赖取共享保护；修改某人资格或某资源 ACL 时，在相应范围取排他保护。组织策略变更仍需广域保护。此处只是候选形态，**继承角色、部门关系、任职、直接 ACL、规则新增/删除、来源/client/session 变更的覆盖还需正式清单**。

Stage 当前 guard epoch 覆盖部分授权集合变动，但身份、trust、client、session 有各自版本；不能直接拿一个 epoch 当所有事实的统一失效编号。角色 revision 内容的缓存也必须区分“发布内容不可变”与“当前角色已停用/撤销”，后者仍要当前检查。

## 7. 建议下一步先交付设计，再做实验

建议设计范围是：**短事务＋明确的授权读者/写者协议＋独立对象和幂等并发控制**，不先增加外部基础设施。

设计需明确四项：

1. 一致性合同：哪些操作必须遵守现有撤权提交顺序；哪些纯展示允许版本化快照。未明确放宽前，继续保留当前合同。
2. 冲突矩阵：每类身份/权限/凭据/业务/投递写者改哪些事实，普通请求依赖哪些范围；锁模式一次确定，禁止隐式升级。
3. 事务内事实合同：哪些查询可以批量读取或在同一锁域复用，哪些期限仍要在最后检查；未来事务外计算只产生候选结果，不能提前披露或造成外部副作用。
4. 实验矩阵：原实现、共享行锁、事务 advisory 锁在相同负载下的吞吐、p95/p99、撤权等待、SQL、锁/MultiXact、取消和重试代价。

先用 PostgreSQL 竞争实验验证语义，再看速度：不同人员/对象真实并行、同幂等键仅执行一次、同对象冲突明确、撤权先行/业务先行、客户端/账号/会话撤销、MFA 收紧、refresh 重复消费、超时取消、死锁和提交未知恢复。负载还需加入真实比例的权限写者；不能仅在没有撤权的读多负载上宣布优化成功。

事务外纯计算＋最终短事务属于成熟的乐观处理方向；最终事务仍应从入口按统一顺序取得保护并重验，不能先锁业务对象，再倒过来补组织保护。现有全组织串行并非长期必要，但“取消锁”也不是本次研究得出的结论。

## 8. 一手来源与适用边界

访问日期：2026-10-26。PostgreSQL 固定为17版、SQLAlchemy 固定2.0文档系列；产品在线文档为当日读取内容，不宣称固定发行版本。来源文本/PDF摘要记录见[研究来源清单](organization-lock-industry-sources-2026-10-26.json)，未把第三方全文收入仓库。

- **[P1]** PostgreSQL 17：[Transaction Isolation](https://www.postgresql.org/docs/17/transaction-iso.html)，§13.2.1、§13.2.3。
- **[P2]** PostgreSQL 17：[Application-Level Consistency](https://www.postgresql.org/docs/17/applevel-consistency.html)，§13.4、§13.4.2。
- **[P3]** PostgreSQL 17：[Row Security Policies](https://www.postgresql.org/docs/17/ddl-rowsecurity.html)，末尾跨表策略竞态示例。
- **[P4]** PostgreSQL 17：[Explicit Locking](https://www.postgresql.org/docs/17/explicit-locking.html)，§13.3.2、§13.3.4、§13.3.5。
- **[P5]** PostgreSQL 17：[Advisory Lock Functions](https://www.postgresql.org/docs/17/functions-admin.html#FUNCTIONS-ADVISORY-LOCKS)。
- **[P6]** PostgreSQL 17：[Routine Vacuuming](https://www.postgresql.org/docs/17/routine-vacuuming.html#VACUUM-FOR-MULTIXACT-WRAPAROUND)，MultiXact 维护。
- **[Z1]** Pang et al., USENIX ATC 2019：[Google 出版页](https://research.google/pubs/zanzibar-googles-consistent-global-authorization-system/)、[原论文 PDF](https://storage.googleapis.com/gweb-research2023-media/pubtools/5068.pdf)，§2.2、§2.4.4、§3.2.5。
- **[S1]** Authzed：[SpiceDB Consistency](https://authzed.com/docs/spicedb/concepts/consistency)。
- **[S2]** Authzed：[Read-After-Write Consistency](https://authzed.com/docs/spicedb/concepts/read-after-write)。
- **[S3]** Authzed：[Writing Relationships](https://authzed.com/docs/spicedb/concepts/relationships)，应用数据同步、Two writes & commit、Streaming commits。
- **[F1]** OpenFGA：[Query Consistency Modes](https://openfga.dev/docs/interacting/consistency)。
- **[O1]** OPA：[External Data](https://www.openpolicyagent.org/docs/external-data)。
- **[Q1]** SQLAlchemy 2.0：[Configuring a Version Counter](https://docs.sqlalchemy.org/en/20/orm/versioning.html)。

原 Stage 数据见[容量报告](p2p3-capacity-2026-10-26.md)。本文没有新的 Stage 性能数字；任何候选的预计收益仍待实验。
