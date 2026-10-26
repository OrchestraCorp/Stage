---
title: Stage 整体架构与工程质量审计
status: audited-current-workspace
date: 2026-10-26
policy_effect: none
---

# Stage 整体架构与工程质量审计

> 本报告保留整改前的发现与证据。用户随后批准全部 A–H，处理结果和新复查见[P2/P3 收尾记录](p2p3-architecture-closeout-2026-10-26.md)；下列旧行号和结构计数用于解释原始发现，当前结构以更新后的代码地图为准。

## 1. 结论

**架构方向正确，安全与一致性基础比新增业务的复用入口更成熟。它具备工程之美的核心——权威清晰、失败语义诚实、关键不变量可验证——但还没有达到职责收敛、扩展轻松、交付可重复的完成状态。**

建议继续保持模块化单体、独立 Runtime Core、PostgreSQL 权威和统一授权/事务链。下一步应收拢真实业务的安全装配入口、把浏览器回归纳入 CI、冻结可复现版本，再用第二条真实业务闭环检验这些改进。当前没有证据支持推倒重写、改微服务、扩建通用插件平台，或为了文件变短直接删除锁和提交前重验。

本轮是当前 Stage 工作区的源码/结构审计，并补做静态、合同与 Runtime 定向复核；没有宣称深审 Maestro、Virtuoso 或 ScoreBook 全仓，也没有重新执行上轮 PostgreSQL/浏览器全量攻击面测试。未证实新的 P0/P1 安全漏洞，下面的 P2/P3 主要是具体的维护、交付和容量问题。**“未发现”不等于完整安全认证。**

总体结构和关键调用链见[全景](../stage-architecture-panorama.md)；逐文件/函数见[完整代码地图](../stage-code-map.md)。

## 2. 用什么判断“工程之美”

| 判断标准 | 当前评价 | 依据 |
|---|---|---|
| 权威与职责清楚 | 较好 | 凭据、来源/成员准入、岗位/对象权限、业务事务分别负责；Redis 不承诺权威撤销 |
| 正确性容易表达和验证 | 较好 | owner/generation/lease；版本/幂等；锁后与提交前重验；unknown 单独建模 |
| 领域逻辑干净 | 较好，但证据有限 | WorkDetails handler 不处理 HTTP、JWT、角色名或外部 I/O；只一条真实业务写闭环 |
| 依赖清晰、改动局部 | 中等 | Runtime 纯净；provider/identity/authz/execution 与 OAuth 之间有应用级交叉、私有方法复用 |
| 扩展代价低 | 尚需证明 | 第二个业务域仍要复制认证、上下文、loader、kernel、锁与结果恢复装配 |
| 运维与交付可重复 | 有基础但未闭环 | PG/Redis/合同门禁已有；浏览器不进 CI，当前实现仍主要在未提交工作区 |
| 抽象与需求相称 | 总体克制，需暂停扩张 | 37 表对应当前事实；纯 Runtime 不是万能插件平台；完整任务/流程/Agent 委托未提前实现 |

这里不给主观的“9 分架构”评分。规模、文件长度、函数数量只是定位复杂度的线索；只有能说明修改风险、故障模式或新增功能成本时才列为发现。

## 3. 代码实况与范围

AST 快照统计：95 个 app Python 文件，15,774 行（含注释/空行），242 个类，612 个函数/方法（含私有、property、协议声明、嵌套闭包）。实际 OpenAPI 25 个 method+path 操作，SQLAlchemy 37 张应用表，11 个迁移 revision，head `c6f3a19d820b`。

- Runtime 1,774 行，约占 app 11%；不能据此称整个服务是一个庞大的自研框架。
- `native_auth` 3,695 行，`identity` 2,120 行，认证 HTTP 1,211 行，合计约45%。这是用户明确选择自研发行器后的实际投入，不是自动等价于过度设计。
- 具体领域 `features` 162 行，当前唯一默认可执行写命令是 `work.details.update`。扩展性还需要更多真实业务来验证。
- `app/runtime` 只依赖标准库和同包代码；业务身份和 SQL 在外部。这一点是实际代码边界，不只是图上的分层。
- `app/core` 是框架辅助，不是 Runtime Core；`providers` 同时放资源适配和应用编排。它是一套模块化单体，不是严格单向的同心层架构。

完整源码摘要与导入清单见[机器快照](stage-architecture-inventory-2026-10-26.json)。导入清单区分顶层、函数内与 `TYPE_CHECKING`；不得把类型依赖或 Python 延迟导入一概报告为启动循环故障。

## 4. 关键发现

### A. P2：真实业务执行的公共入口尚未收敛

证据：[HumanWorkService](../../app/providers/work.py#L74) 的 update/get_operation/get_item 重复身份验证、ExecutionContext、loader、kernel 装配；查询在[第178行](../../app/providers/work.py#L178)与第191行调用 `CommandKernel._authorize` 私有方法。

内部通用 [CommandService](../../app/providers/execution.py#L26) 又没有同一套 identity_validator 装配合同，带真实认证 evidence 时会被内核安全拒绝。它未默认开放，当前不是越权漏洞；问题是开发者无法拿一个稳定公共入口加入新领域操作。

**影响**：每个新功能都要复制安全顺序，漏掉返回前到期、当前角色、锁租约或异常恢复的风险增大。

**最小改进**：公开一个窄的受保护读取入口，再提供人员命令 kernel 装配工厂；保留领域 handler 和 action/事实 loader 的显式定义。用第二个真实领域验证是否减少重复，不建设通用查询 DSL 或万能 service 基类。

### B. P2：认证 HTTP 文件职责集中，修改审查面过大

[app/api/native_auth.py](../../app/api/native_auth.py) 1,211 行，包含参数/Basic/Bearer解析、Origin/Cookie/CSP、HTML与自动转交、协议响应、Discovery/OpenAPI、OAuth 路由及账户 JSON API。

**影响**：协议安全边界、浏览器兼容和产品文案在同一修改单元；改一处页面需要理解很多不相关的路由约束。问题不是数字超过一千，而是变化原因明显不同。

**最小改进**：分成少量同层文件：transport/security helpers、浏览器 views、OAuth endpoints、account endpoints；保持单一验证实现和同一合同。拆分前后跑 Authlib、Chromium 和 PG 定向回归，不引入模板平台或新的 Web 框架。

### C. P2：真实浏览器验证尚未成为持续交付门禁

[Chromium 用例](../../tests/integration/test_native_oauth_browser.py#L27)要求 `TEST_DATABASE_URL` 和 `STAGE_PLAYWRIGHT_MODULE`；[GitHub workflow](../../.github/workflows/ci.yml#L47)与[GitLab workflow](../../.gitlab-ci.yml)没有安装和设置浏览器依赖。这些用例会固定跳过，而前几轮确实曾由真实浏览器发现 Origin/CSP/Cookie 问题。

**最小改进**：增加独立的 Chromium CI job，使用专属一次性 PG，覆盖登录/恢复、SSO/同意、form_post、跨站退出；上传脱敏结果，不保存凭据和认证页面快照。只需把已有成功脚本接入门禁，不必重写测试。

### D. P2：当前实现需要冻结为可复现交付版本

本轮检查的 Git HEAD 仍是 `80447174c2acdccfb17455cbf28727b10ee2c6cf`，身份/OAuth/迁移等有大量工作区修改和未跟踪文件。[开发 service](../../deployment/dev/orchestra-stage-dev.service#L11)直接挂载该工作区。这符合云开发设备用途，但不等于从该 HEAD 就能构建出当前已验收服务。

**最小改进**：下一次交付冻结代码提交、迁移 head、uv.lock、制品与验证记录；CI 验证该确切版本。当前报告绑定文件摘要，不擅自把历史 HEAD 称为新实现版本。本轮没有替用户执行提交/合并/发布。

### E. P2：统一登录扩大了组织 guard 的争用范围

[acquire_account_lock](../../app/native_auth/credentials.py#L65)取得主体/账号排他锁和组织 guard；[authenticate_account](../../app/native_auth/oauth.py#L631)用于 UserInfo、会话管理等读取。profile UserInfo 还有两次事务复核。不同员工的认证流量可能竞争同一个组织串行点。

这是现行撤权一致性取舍，不是“锁写错了”。已有[PG 小样本基线](p1b-human-work-baseline-2026-10-25.json)和并发顺序测试；**尚缺实际企业负载目标与生产容量验收**，不能说完全未做过并发测试。

**最小改进**：先测登录峰值、锁等待/持锁时间、SQL次数、共享出口与代理；再分别设计可证明撤销一致性的读取/写入协调。不要直接移除 guard、改用 Redis 权威或缓存掉当前权限检查。

### F. P3：已成为公共职责的基础协议仍藏在私有/专用模块中

[client_administration](../../app/native_auth/client_administration.py#L14)、原生安装、授权管理、MFA 管理都导入 `identity.administration` 的 `_admin_lock/_journal/_operation_transaction/_replay` 等私有帮助函数。代码复用方向对，但接口已事实公共化，归属还停在 bootstrap 模块。

同时，[validate_native_session](../../app/native_auth/oauth.py#L139) 是当前会话事实验证，却放在 OAuth 协议服务内，被 identity、credentials 和 sessions 跨域调用；[credentials](../../app/native_auth/credentials.py#L600)用延迟 import 进入 OAuth。这里不是已经发生的运行时循环导入崩溃，OAuth 对 credentials 的 import 位于 `TYPE_CHECKING`。

**最小改进**：提取小型公共的 control operation/journal 支持模块、current session validation 模块；把 `KeycloakProfile` 的共享验证配置中性命名。验证端以后可持只读公钥视图，当前同进程私钥能力不伪装成隔离沙箱。无需通用认证插件框架。

### G. P3：动态替换的历史记录没有容量边界

[RuntimeHost](../../app/runtime/host.py#L507)持续将组件写入 `_records`，`replace()` 关闭后再 load；observations 也持续追加。虽然成功排空会清空旧服务 binding，但 disposed 记录仍可持有 `PluginSpec.setup` 闭包与配置。

独立只读内存复现：关闭/替换后删除旧 handle 并 GC，旧 setup 捕获对象仍由历史记录保持，snapshot 留有两个代次。当前固定启动装配没有高频动态安装入口，因此不外推为现网内存事故。

**最小改进**：开放长期热替换前，将成功卸载记录压缩成不保留资源的有界诊断摘要，并给 observations 定上限；清理失败的所有权和诊断仍必须保留。别通过删除所有历史掩盖未完成清理。

### H. P3：测试模块之间开始形成隐藏的共享库

只读扫描中，89 个测试 Python 文件内41个直接 import 其他 test 模块，共110条跨 test import。例如[并发测试](../../tests/native_auth/test_command_concurrency.py#L19)复用其他测试中的 fixture，[migrated_sessions](../../tests/integration/test_human_work_live.py#L53)也被新协议集成复用。

**最小改进**：把通用 builders、时钟、PG 锁观察、迁移 fixture 放入 `tests/support` 与分域 `conftest.py`，测试文件聚焦行为断言。当前 PG fixture 每例重置同一个获批测试数据库，串行运行正确；开启 xdist 前需要按 worker 隔离数据库/schema。

## 5. 本轮已修正的轻量问题

- 原生运行手册“未实现多客户端/SSO”的旧描述已与现行受控多应用事实对齐；当前架构表格中的“8 张原生表”更正为11张。
- 完整检查发现 `tests/native_auth/test_audit_credentials.py` 文件末尾多一个空行，导致现有 CI 的 format gate 失败。本轮只删除该多余空行，未改变测试逻辑。

上述修订和本轮文档不改变 HTTP、数据库或运行服务行为。A–H 的结构改进没有在审计过程中顺手大规模实施，用户可以按影响顺序选择下一轮范围。

## 6. 应保留的设计

1. **身份不是权限。** 本地账号、OIDC client、用户同意、组织成员和当前任职有各自权威；登录成功不能直接成为业务授权。
2. **领域处理器保持简单。** [update_work_details](../../app/features/work/details.py#L80)只表达领域约束和修改，无 HTTP/JWT/角色名分支，无外部 I/O。
3. **原子提交边界明确。** 业务、幂等结果、认证/授权审计与可选 outbox 由内核同事务提交；handler 不能随意取得根事务提交权。
4. **错误和外部副作用诚实。** commit unknown、投递 unknown、持久对账和租约 fencing 没有被包装成虚假的 exactly-once。
5. **Runtime 有真实隔离边界。** 独立 stdlib Core 不引入 SQL/身份事实；effect/lease/drain 管理进程资源，生命周期清理不撤销持久事实。
6. **默认范围克制。** 没有把历史 ClaimWork/SubmitDelivery/AcceptDelivery DTO 当已实现任务产品；WASM、持久工作流、Agent 委托、生产连接器保持后置。
7. **验证有机器证据。** OpenAPI 对实际应用比较、迁移防丢事实、PG/Redis 真库门禁、46个 Runtime 场景映射、Authlib互操作与可运行的浏览器脚本，均优于仅靠文档或测试数量。

安全关键代码里“读取后再校验”“flush 后再看时钟”有明确时间语义，不应为了 DRY 删除；应把顺序封装成有合同的公共入口，而不是把所有校验合并成一次。

## 7. 下一轮建议顺序

| 顺序 | 工作 | 完成的判据 |
|---|---|---|
| 1 | 冻结可复现基线、补 Chromium CI | 从指定提交与锁文件可构建；浏览器关键路径在 CI 实跑而不是 skip |
| 2 | 公开受保护读取/人员命令装配入口 | 第二条业务操作不复制 evidence/锁/期限安全胶水，原撤权和到期测试继续通过 |
| 3 | 拆认证 HTTP 文件，提取共用会话/管理协议 | 变化原因分离、无跨域私有方法依赖，HTTP/浏览器/事务行为不变 |
| 4 | 用真实业务/连接器与容量目标验证 | 第二业务域和首个 sink 证明组合价值；锁优化有负载数据和撤销一致性证据 |
| 5 | 再开放更强动态能力 | 历史记录有界、换代/失败清理可恢复；不为模仿 Cordis 先建通用平台 |

## 8. 本轮验证和证据限制

本轮重新完成 AST/模块/符号/HTTP/模型核对，最终 `tests/runtime_core`＋`tests/contract/test_openapi_contract.py` **72 项通过**（其中 Runtime 67 项）；Ruff lint、207 个 Python 文件格式、Mypy 204 个文件与 OpenAPI 合同检查通过。70 个 Markdown 文件、1,713 个本地链接、18 个 Markdown 锚点及围栏检查通过；95 个模块/612 个函数定义与 AST 一致，全景内联图与 Mermaid 源完全相同，SVG/PNG 已实际渲染。

上一轮全量证据是 PG 1383 passed/9 skipped、SQLite 1332 passed/52 skipped；最后 OAuth/原生/真实浏览器定向组551 passed。它们是已有历史验证，**不是本轮重复执行的新计数**。没有核验远端 CI 执行状态，也没有把源码检查等同生产容量或完整安全审计。源码清单带工作区每文件摘要，不仅引用旧 HEAD。
