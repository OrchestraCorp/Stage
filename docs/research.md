---
title: Stage 可组合架构调研依据与决策
status: research-record
policy_effect: none
research_date: 2026-10-23
---

# Stage 可组合架构调研依据与决策

本记录把已观察的事实、来源作者的主张和 Stage 的工程选择分开。核查使用论文原文、本地固定 DSH/Cordis 源码、当前 Stage 代码及 Python / FastAPI / SQLAlchemy 官方文档。没有把社区项目 README 的功能清单当作等价性证明，也没有安装或运行社区 Python Cordis 移植包。

2026-10-24 增补：一期按Core、真实身份协议、受控WASM及最小签名准入重排，新增依据见[表达修正记录](design-review-2026-10-24.md)、[身份规范](identity-and-authorization.md)和[WASM宿主](wasm-host-design.md)。本文件保留原研究日期和来源快照，不将静态核对或历史底板测试记为新规格已通过。

<a id="current-baseline"></a>
## 1. 当前代码基线 [L1]

Stage 参考提交：`271d175546901832fd699bc570745901a617b03b`。开始本次设计前工作区干净；本次只增改设计、文档入口和图表，没有新增 runtime、业务 API、数据库迁移或包依赖。

| 检查对象 | 当前事实 | 代码依据 |
|---|---|---|
| 服务 | FastAPI 生命周期、请求 ID、脱敏错误；公开业务路由只有 `/health` | [app/main.py](../app/main.py)、[health.py](../app/api/health.py)、[空业务 router](../app/api/router.py) |
| 持久模型 | principal、role_assignment、relation_tuple、work_item 四类表 | [models](../app/models/) |
| 授权 | 纯 Python RBAC / ReBAC / ABAC，内置七个能力点、四个角色，委托 / 时间 / 分类 / 版本检查 | [permissions.py](../app/authz/permissions.py)、[decision.py](../app/authz/decision.py) |
| 业务写 | ClaimWork / SubmitDelivery / AcceptDelivery 只有输入结构 | [commands.py](../app/features/work/commands.py) |
| 审计与消息 | 参考提交时 AuditEntry、OutboxMessage、IdempotencyKey 只有结构或格式检查；本轮后新增内部 CommandKernel/Execution/Delivery 实现 | 历史基线见 [audit.py](../app/core/audit.py)、[outbox.py](../app/core/outbox.py)；当前实现见 [app/execution](../app/execution/) 与 [app/delivery](../app/delivery/) |
| 存储 | SQLAlchemy async、UTC 与精确字符串类型、两条迁移；请求不自动 commit | [db.py](../app/core/db.py)、[storage/types.py](../app/storage/types.py)、[migrations](../migrations/README.md) |
| 工程检查 | 有测试、lint、类型及 OpenAPI 检查；历史底板测试通过不代表新设计已实现 | [审查记录](framework-review-2026-10-19.md)、[tests](../tests/) |
| CI | GitHub 托管；仓内有 GitLab CI 配置，尚未见 GitHub workflow | [README](../README.md)、[.gitlab-ci.yml](../.gitlab-ci.yml) |

先前会话在 2026-10-22 验证了 98 项测试、Ruff、格式、Mypy 和契约检查；依赖导入受云占位文件影响，最终通过本地匹配版本 uv 缓存完成。这里保留其历史性质，本次文档任务没有重新运行该套测试。MySQL / PostgreSQL 实例、容器和远端流水线没有新的实跑证据。

当前自审保护也不完整：验收策略校验 reviewer/owner 关系、状态与版本，但没有提交者 / 承诺责任人与 reviewer 的职责分离条件；现无业务 API，因此该发现是接入前必须补齐的设计缺口。新增组织字段、可信 loader 和独立验收见[一期方案](phase-1.md)。

## 2. 论文 [S1]

来源：[arXiv:2608.25512](https://arxiv.org/abs/2608.25512)，*A Programming Paradigm for Spatiotemporal Composability*，Yifan Shi、Wei Zhang、Tianyi Cui，2026-08-26 提交。本次读取完整 PDF 的相关章节并核对关键算法；文件共 92 页，SHA-256 为 `390775dbc9debdcf2ed1b076eed013387ca057630be3cb594617b2b742e48cf0`。使用 hash 标识阅读快照，不把未固定的最新版本作为永久一致性基线。

| 原文位置 | 核对结论 | 对 Stage 的影响 |
|---|---|---|
| §3 与 §5.1，p57–60 | effect/coeffect 经统一 Context 管理；原子 inverse 的正确性仍由提供者承担 | 登记资源归属并组合清理；无法自动反演任意代码 |
| §5.1.3，p61–63 | 依赖目标与激活绑定分开；异步转换要收敛后再处理新目标 | 显式 generation、committed view、过期结果检查 |
| Algorithm 5，p63 | provider 整体恢复前等待依赖者退出；文字明确顶层 effects 可以并发 | 不将论文概括为“所有清理全局 LIFO”；Stage 另声明自己的清理顺序 |
| §5.2，p64–68 | Loader 与 HMR 建立在 Core 之上 | Core / Loader / HMR 分层验收 |
| §6.1，p70–71 | 系统边界之外的发送效果不能靠资源关闭恢复 | 数据库事务、outbox、外部回执与补偿另行实现 |
| §6.3–6.4，p72–74 | 语言无关；Python descriptor 可作为访问媒介；不可信代码仍需隔离边界 | Python 原生实现可行；Context 不是 sandbox |
| §6.5–6.6，p74–76 | 依赖环与接口版本 / 兼容性各需处理 | 有环诊断、版本化 ServiceKey 与行为测试 |
| §5.3，p70；§7.4，p82 | 案例是单生态观察；已有动态服务模型是重要前例 | 不作性能优越或社区替代品已等价的推断 |

以上为阅读结论与设计映射，不是我们完成了论文形式证明。任何“安全可撤回”声明都要限定在受管理操作、正确原子 inverse 和已验证并发语义内。

## 3. DSH/Cordis 实际参考 [L2]

本次核查本地 Maestro 提交 `6f7124baabca582d43ce84fd59d2f48e4b790763`。涉及的 `vendor/cordis`、`vendor/loader`、`vendor/README.md` 没有未提交改动；整个 Maestro 工作区仍可能存在其他开发变更，不能以 HEAD 代表所有工作区文件。

仓内包名为 `@deepseek-ai/cordis`，包版本 **4.0.5**；vendor 来源表记录上游 **Cordis 4.0.0-rc.7**、提交 `56b3d4f725681cf4556c1a8695a709cc3b6eed74`。这两个版本号分别代表 DSH 发布包和上游来源，不能混用。DSH 还包含重入卸载、异步清理归属、延迟配置解析等本地修复。

源码定位以下述固定仓库为根；不要求 Stage checkout 旁边必须存在 Maestro。复核时取得同一 commit 的对应文件并核对 hash。上游项目入口：[Cordis](https://github.com/cordiverse/cordis)；DSH 的具体行为以本表代码快照为准。

| 文件 | 核查内容 | SHA-256 |
|---|---|---|
| `vendor/cordis/src/context.ts` | 派生 context、按 key 的 isolate / intercept | `96b388162d6013c1898de61e35f28917abb93809de24174980f2a1a348d0b115` |
| `vendor/cordis/src/reflect.ts` | 受声明读取、实时 get、provide / notify / 撤销等待 | `6847b9781044023d65aebb5896a873b9f0a3d777eaf7fa0dd2a7a408eb6b74a6` |
| `vendor/cordis/src/fiber.ts` | 资源归属、代次、状态、清理顺序 | `b7661d6c19be76cef6b97d88719a8112c0f8b5abe65bd9e7d5947773fd4b96d4` |
| `vendor/cordis/src/events.ts` | emit / parallel / serial / bail / waterfall 及 listener 归属 | `9aff93dcfcad98aebb692c19c36d3bf034a96f9085bc66900a997cda3f79e6fe` |
| `vendor/cordis/src/utils.ts` | caller-context tracing、服务内资源归属 | `8fe273424583d21267ef13f248840e97454d09d836a0ac08323a9482c2069263` |
| `vendor/loader/src/config/entry.ts` | 配置协调和 Fiber 身份 | `6a061f1d3c2cdaac19409bf8ec6956ec5a0a457c4f1689be658d50af7c9ee294` |

### 3.1 三处不能直接等同的语义

1. **撤销屏障**：vendor 的服务撤销等待在 `reflect.provide()` disposer 内，Fiber 顶层 effects 并发启动；不能据此保证其他 provider 资源一定晚于消费者退出。Stage 采用整体 drain 屏障，并单列偏差。
2. **清理顺序**：vendor 单个 effect 内逆序串行，Fiber 顶层并发。Stage 为首期可解释性采用同 owner 反序；这是自身规则，不是论文或全部 Cordis 版本的共同标准。
3. **配置事务性**：vendor 说明 Loader 变更是 eager/non-transactional，激活失败可能部分生效。Stage 长期配置发布必须显式记录中间状态、observed revision 和恢复结果，不能把“有 disposer”写成完整事务回滚。

详细 API 范围、原七项偏差和 34 个验收场景集中在 [Runtime 规格](runtime-conformance.md)。2026-10-25 又以同一固定源码复核并补充[框架实施规格](framework-foundation.md)，追加在途事件/严格就绪的 D-08、D-09 及 12 个 S1 边界场景，仍未执行。Stage 不复制 Node 模块缓存操作、TS declaration merging 或 `!!js` 执行能力，也不宣称原 TS 插件可以直接装入 Python。

## 4. Python 与现有技术栈依据

以下官方文档在本轮核查；版本文档限定 Python 3.12 和 SQLAlchemy 2.0，避免使用新版本语法冒充现有最低环境能力。

| ID | 官方来源 | 支持的结论 | 不支持的推断 |
|---|---|---|---|
| S2 | [contextlib / AsyncExitStack](https://docs.python.org/3.12/library/contextlib.html#contextlib.AsyncExitStack) | 动态登记同步 / 异步退出操作，需要明确关闭 owner | 单靠资源栈就拥有响应式依赖图或完整错误治理 |
| S3 | [asyncio tasks / TaskGroup / cancellation](https://docs.python.org/3.12/library/asyncio-task.html#task-groups) | 任务组有失败传播；取消是协作式；必须跟踪并等待任务 | 超时等于外部动作停止；一个全局组能隔离所有插件故障 |
| S4 | [importlib.reload](https://docs.python.org/3.12/library/importlib.html#importlib.reload) | 旧引用、已有实例及模块字典需要额外处理 | reload 等于原子 HMR 或业务状态迁移 |
| S5 | [contextvars](https://docs.python.org/3.12/library/contextvars.html) | 任务上下文与 set/reset 可承载诊断信息 | 隐式上下文是可信身份来源或跨任务同步机制 |
| S6 | [SQLAlchemy AsyncSession 并发](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#using-asyncsession-with-concurrent-tasks) | 每个并发 task 使用独立 AsyncSession | 一份共享 Session 可以安全作为全局 provider |
| S7 | [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/) | 应用入口集中管理启动和关闭资源 | `include_router()` 已自带可靠动态撤销和契约更新 |
| S8 | [FastAPI deployment concepts](https://fastapi.tiangolo.com/deployment/concepts/) | 多 worker 是独立进程，内存通常不共享 | 更新一个 registry 就等于所有进程一致生效 |

## 5. 现有框架的借鉴范围

本次目标是定义 Stage 所需语义，不按项目名称或年龄评选“成熟版 Python Cordis”。

| 路线 | 可参考能力 | 本轮取舍 |
|---|---|---|
| [iPOPO/Pelix](https://ipopo.readthedocs.io/en/stable/refcards/ipopo_decorators.html) | provided/required 服务、validate/invalidate、动态绑定 | 与 reactive dependency 部分重叠；没有把这些机制等同于完整异步 effect/Core 规范，也不立即增加依赖 |
| [Dishka](https://dishka.readthedocs.io/en/stable/) | DI、作用域、资源 finalization 与 FastAPI 集成 | 可借鉴业务依赖组织；不以 DI 就绪作为本规格已满足的证明 |
| 社区 Python Cordis 移植 | 可作为实现对照候选 | 先前仅做过源码抽查，未运行一致性测试；本轮不据 README 或版本号作采用承诺 |
| Python 原生小内核 | 精确控制本规格所需的生命周期和诊断 | 2026-10-24 已明确自研方向；成本是自己维护并发、清理、兼容性与测试，不是零成本封装 |

领域命令只依赖公开接口，内部实现演进必须通过相同场景验证。当前实施方向是自研组件内核与自研流程运行器；其他项目作为边界案例和语义研究来源，不构成外部框架接入计划。具体决定见[愿景与自研方向](vision-and-runtime-direction.md)。

## 6. 本轮决策与证据强度

| 决策 | 类型 | 理由与复核时机 |
|---|---|---|
| 保留现有 Python / FastAPI / SQLAlchemy 底板 | 工程选择，代码边界已观察 | 生命周期创建点集中；第一期可小范围迁入 provider |
| ComponentContext 与 ExecutionContext 分开 | 工程选择 | 长期组件和每次授权事实生命周期不同；替代旧统一 RuntimeContext 提案 |
| P1 固定单worker可信宿主＋受控WASM runner子进程 | 范围选择，2026-10-24修订 | 验证Core/身份/纯计算/提交边界，不等于多ASGI worker或分布式协调 |
| 整体 drain、显式代次、清理失败报告 | 工作规格约束 | 对照论文 / vendor 后明确偏差；以一致性测试验证，不宣称形式证明 |
| 一期每租户 authorization guard | 业务并发设计 | 为撤销与写命令提供共同锁点；吞吐代价及两库锁行为待实测 |
| 事务内状态 / 幂等 / 审计 / outbox | 领域一致性要求 | 2026-10-24 已有内部第一版实现及 SQLite 测试；尚未接入 HTTP/真实身份/生产 sink，MySQL/PG 真实并发仍待 CI |
| 配置重组早于代码 HMR | 阶段选择 | 避免将模块 reload 的限制变成一期运行风险；收益明确后再评估 |

未验证事项：新runtime Core 34个场景、新增身份/WASM/对象/联调场景、真实协议adapter、制品准入、一期评估API、生产认证与密钥、MySQL/PG真实并发、多租户吞吐及多节点恢复。2026-10-24 已运行内部执行/投递/迁移单测；这些通过不代表上述未实现边界已完成。必须给出环境、参考commit、执行命令、失败和差异。

## 7. 文档验证范围

本轮应检查：新目录入口完整、相对链接与锚点有效、Mermaid 可解析并渲染、图上阶段和箭头与正文一致、参考 SHA 与本地快照相符、旧提案有修订指向、git whitespace 无错误。文档检查不会运行生产迁移、外部连接器或更改企业配置，也不替代代码验收。
