# Stage

新增[首个人员管理切片](docs/human-administration-first-slice.md)：首管提名／本人接受、现有人员目录、有限普通任职授撤、原键恢复和管理审计。[运行手册](deployment/identity/human-administration.md)与 OpenAPI 同步；Maestro 管理页面、员工激活／离职和多人管理仍按后续切片交付。

当前代码导航：[架构全景](docs/stage-architecture-panorama.md) · [完整目录与函数](docs/stage-code-map.md) · [整体工程审计](docs/reviews/stage-engineering-audit-2026-10-26.md) · [P2/P3 整改复查](docs/reviews/p2p3-architecture-closeout-2026-10-26.md) · [并发优化与复查](docs/reviews/concurrency-optimization-2026-10-26.md)。

一期协作模式为 **人员—公司—人员**：人员持有成员资格、任职和业务责任，Agent 只作为人员监督下的工具；手动与工具辅助操作共用业务接口。独立 Agent 身份、承载绑定及委托链不属于一期门槛。已按用户确认恢复开发，以既有工作项的标题/说明编辑落实[一期协作边界](docs/people-company-collaboration.md)。

Stage 面向**企业私有化部署**：每套实例拥有一个组织授权根、专属数据库和固定部署环境。组织授权根是信任与治理边界，不等同于一个法人公司；集团、子公司、部门等业务组织单元按需建模，部门、团队、项目的授权范围使用 scope。客户端不选择组织。当前业务合同统一为 `/api/v1`，身份入口为 `GET /api/v1/identity/me`，人员可通过 `GET /api/v1/identity/assignments` 发现本人当前任职；边界见[组织授权根语义](docs/organization-root-semantics.md)和[私有化部署与 API v1](docs/private-deployment-api-v1.md)。

2026-10-25 冻结基线 `8044717` 已完成组织根语义与无损迁移：当时 PostgreSQL＋Redis 487 项、SQLite 464 项、在线冒烟 5 项、真实 IdP 18 项通过，见[基线验收](docs/reviews/organization-root-semantics-2026-10-25.md)。[P1b.2](docs/reviews/p1b2-authorization-2026-10-25.md)增加单任职/角色版本/scope/resource/direct ACL；[P1b.3](docs/reviews/p1b3-human-work-2026-10-25.md)接通真实人员认证、已有工作项读取/编辑及结果恢复。各轮验收独立记录，不累计或改写历史计数。

Stage 是 OrchestraCorp 的可信组织协作运行底座，用“舞台 / 演出台”指代组织正在发生的工作。OrchestraCorp 的长期愿景是 AI 驱动、安全可靠的分布式工作协作平台。

P1b 的最小人员路径 Stage 收尾见[架构复查与并发基线](docs/reviews/p1b-closeout-2026-10-25.md)、[I1 覆盖矩阵](docs/reviews/p1b-identity-coverage-2026-10-25.md)和[Maestro 联调合同](docs/maestro-stage-p1b-handoff.md)。[P1b.4](docs/reviews/p1b4-assignments-mfa-2026-10-26.md)已提供本人任职发现、组织统一登录 MFA 策略及其受控配置，用户选定密码＋TOTP；真实隔离 IdP 与 Stage 链路已有证据。随后[原生认证模块](docs/native-authentication-design.md)已实现本地密码＋TOTP、因素绑定/恢复、账户安全与最小 OAuth/OIDC；[独立审计](docs/reviews/native-authentication-audit-2026-10-26.md)已修复因素重绑授权和到期边界，重绑必须使用单次证明；原生会话退出会立即使 Stage 当前身份失效。随后[受控多应用 OAuth](docs/oauth-functional-coverage.md)补齐客户端管理、SSO/同意、标准退出和本人会话/应用授权管理。**Maestro 桌面交互与生产接入仍待验收，整体 P1b 尚未退出。** 旧外部 IdP 的本地撤权不等于自动感知远端注销，任职由管理员显式授予，再通过本人查询选择，不默认授予业务权限。业务模块可按受信组件组织，通用能力目录、配置化安装/停用和持久流程仍属后续能力；[文档复查](docs/reviews/documentation-alignment-2026-10-25.md)记录现行与历史依据。

已确定采用自研组件运行时和自研业务流程运行器，参考 Cordis 的组件思想，由 Stage 自己定义一致性工作规范。Graphon / Dify 仅作为研究参考；愿景、职责及演进边界见[自研运行时方向](docs/vision-and-runtime-direction.md)。

2026-10-25 已完成[一期框架实施规格](docs/framework-foundation.md)的 P1a.1～P1a.3：Core、生命周期/事件、PostgreSQL/Redis provider 与 FastAPI 装配。34 个 Core 场景与 12 个边界场景的映射验收为 46/46，真实服务全套 324 项测试通过；复查与只读冒烟见[代码架构复查](docs/reviews/code-architecture-review-2026-10-25.md)。云设备服务已运行，见[运行说明](deployment/dev/README.md)。

产品源码由 GitHub 私有仓库 `OrchestraCorp/Stage` 管理；企业 GitLab 连接器和企业仓库权限仍按业务架构单独建设。

后续实现的统一入口见 [docs 设计目录](docs/README.md)：一期优先建设组件运行时、真实人员身份与授权、受控 WASM 和签名准入。P1a 与身份读取保留各轮验收证据，P1b.2/.3 已分别接入数据库单任职授权及人员工作项元数据命令，见[P1b.3 实施记录](docs/reviews/p1b3-human-work-2026-10-25.md)。完整任务协作按 P2 推进；独立 Agent 身份与委托链后置，ServiceGrant 按真实技术授权需求另案。

2026-10-25 确定长期使用 **PostgreSQL 17＋Redis 8**。PostgreSQL 是唯一主库；Redis 只承担可重建缓存、非权威短期协调和可丢失唤醒。当前授权、幂等、outbox、业务租约及提交结果仍以 PostgreSQL 为准，完整边界见[存储策略](docs/storage-strategy.md)。

[组织运行与热协作架构初稿](architecture.md)从权威边界、六个能力域、核心对象、任务闭环、Agent 执行、GitLab 接入和首版切片展开设计。当前包含设计文档、工程底板与开发身份切片；生产企业身份、GitLab 和企业运行环境仍待接入。

[z-pool 参考实现分析](reference-z-pool.md)记录了从现有后端实现中吸收的工程方法：单一真源、权限点、状态拆分、原子承接、幂等、审计、outbox、未知结果和契约测试。它不把 z-pool 的供应商资源池业务直接复制到 Stage。

[可组合后端框架设计](composable-runtime-design.md)保留早期提案；新的[目标架构](docs/architecture.md)将组件 `ComponentContext`、每请求 `ExecutionContext` 及授权输入分开，并区分运行资源清理与持久业务事务。设计文档不单独构成验收，P1a 实际完成范围以[验证记录](docs/reviews/p1a-validation-2026-10-25.md)为准。

[OA 能力组合策略](composition-strategy.md)明确采用“预建能力模块 + 声明式编排 + 受控扩展”：Maestro 负责理解和提出组合蓝图，Stage 负责验证、执行和留痕，首版不加载 Agent 生成的任意后端代码。

[RBAC、ABAC 与 ReBAC 授权模型](authorization-model-design.md)说明三种授权模型如何分工，并给出沿用 z-pool-v2 权限代码、审计、幂等、outbox 和契约测试习惯的迁移路线。

## 工程底板

本仓库已初始化第一版 Python/uv 工程骨架，沿用 z-pool-v2 熟悉的 `app/core`、`app/authz`、`app/models`、`app/features`、`contracts`、`migrations`、`scripts` 和 `tests` 分层。当前已提供：

- FastAPI 应用入口、独立 Core、`/health` 与受管依赖 `/ready`。
- 固定角色版本的明确 action 集合与单任职选择；不跨任职自动并权。
- 受保护的本人任职分页查询，只返回当前人员、成员经历、有效角色版本和 scope 下的选择；不返回汇总权限。
- 组织统一的 human 登录 MFA 策略：固定 profile/version、ACR/AMR、auth_time 新鲜度和提交前重验；受控 dev/test 配置有版本、幂等及审计。密码＋TOTP 由 IdP 执行，见[操作说明](docs/login-mfa-development.md)。
- 绑定固定成员经历、有效时间和权威来源的直接 ACL；任务 assignee/reviewer 仍需领域 loader。
- 分类、对象版本、主体/委托状态等 ABAC 固定谓词。
- 可解释的授权决策结构。
- SQLAlchemy 异步数据库、模型和 Alembic 入口。
- PostgreSQL 异步数据库入口；SQLite 作为本地迁移/快速测试选项，MySQL 退出受支持配置。
- UTC 时间类型、精确标识符比较和前向迁移预检。
- 应用生命周期、请求 ID 和脱敏错误响应。
- 持久化命令执行、组织 guard、幂等结果、认证/授权审计和 outbox 投递 worker；已接入受保护的工作项元数据编辑 HTTP，WASM runner 和生产 sink 按后续阶段实施。
- `GET /api/v1/work-items/{work_item_id}`、`PUT /api/v1/work-items/{work_item_id}/details` 与原幂等键操作结果查询；只处理已经登记且当前获授权的工作项，不提供任务创建、分派、交付或验收。
- 原生密码＋TOTP/最小 OAuth/OIDC 与兼容的固定 Keycloak access-token profile、企业认证来源准入、主体与当前成员读取；离线 dev/test bootstrap、版本化撤销和可查询操作结果。实际验证范围见本轮实施记录。

开发设备使用 PostgreSQL/Redis；本轮原生认证实现与切换方式见[运行手册](deployment/identity/native-authentication.md)，原有 Keycloak 留作迁移参考；生产企业身份、GitLab 和企业配置尚待接入。依赖使用 Python 3.12 和 `uv sync --locked` 固定。

## 本地检查

```bash
uv sync --locked
uv run pytest
uv run ruff check app tests scripts
uv run ruff format --check app tests scripts
uv run mypy app tests scripts
uv run python scripts/check_contract.py
```

`/health` 的处理逻辑不查询数据库，但默认应用启动必须连接 PostgreSQL 并核验当前迁移 head。运行默认服务前设置 `STAGE_DATABASE_URL` 并完成迁移；隔离测试里只调用健康路由不表示真实服务可在无数据库时启动。

生产数据库使用 `postgresql+asyncpg`，开发快测可用 `sqlite+aiosqlite`。MySQL 历史迁移分支保留，不再代表受支持配置。PostgreSQL 和 Redis 的业务边界不能靠双写原子承诺连接：业务、命令结果、审计与 outbox 先在 PostgreSQL 同事务提交，再发送可恢复的失效通知或唤醒。

应用存储层统一 UTC aware 时间和精确标识符比较，PostgreSQL 会话显式使用 UTC、READ COMMITTED。迁移、锁、撤权竞争和幂等正式门禁以 PostgreSQL 17 真库为准；SQLite 不证明生产并发正确性。Redis 缓存必须隔离部署环境、组织及主体/作用域、核验当前授权版本并定义超时/不可用策略，清空 Redis 不得丢业务事实。

根目录 `compose.yaml` 的开发服务约定为 PostgreSQL 17（`127.0.0.1:15432`）和 Redis 8（`127.0.0.1:16379`），从 `.env.example` 准备本地配置。Core 装配 PG provider；显式配置 Redis URL 后装配 Redis provider，默认不作为整体就绪的必需项，`STAGE_REDIS_REQUIRED=true` 可将其设为必需。缓存/队列业务仍需各自集成；`/health` 始终只检查进程存活。

本地服务启动及 Redis 探针示例（仅虚构开发凭据）：

```bash
docker compose up -d
STAGE_DATABASE_URL=postgresql+asyncpg://stage:stage-dev-postgres@127.0.0.1:15432/stage uv run alembic upgrade head
TEST_REDIS_URL=redis://:stage-dev-redis@127.0.0.1:16379/0 uv run python scripts/check_redis.py
```

Redis 开发服务关闭持久化，内存上限 256 MiB，采用 `noeviction`；容量满时的写失败须由使用方处理。探针只操作随机 key，不执行 FLUSH。GitHub Actions 为主 CI，GitLab CI 保留镜像模板；本轮 PostgreSQL/Redis 目标取代历史双库门禁，不据配置文件宣称远端执行通过。

生产环境必须显式设置 `STAGE_DATABASE_URL`，不会自动采用默认连接，也不允许 SQLite；生产 `/docs`、`/openapi.json` 默认关闭。配置对象的 repr、普通序列化及校验错误文本隐藏数据库 URL；不要记录配置的原始输入或底层异常对象。`/health` 始终仅为 liveness，不表示数据库已可用。

授权内核当前只接受内置策略版本 `stage-authz:builtin-v1`。`AuthenticatedAuthorizationLoader` 将真实人员认证证据与数据库单任职授权连接起来：先取 trust/principal 共享锁，再取组织 guard，读取固定成员经历、角色版本和 WorkItem ACL，提交前重验认证状态与时效。旧未映射行不授予权限，角色名称不隐式扩权；内部 `ExecutionContext` 不能来自请求 body，身份读取响应也不是权限票据。元数据编辑要求显式授予 `task.metadata.update`、对象 `owner_of`、draft/open 状态和预期版本；原角色不会自动增权。独立 Agent/委托与通用 ServiceGrant 继续后置。开发安装、角色版本与接口说明见[授权开发操作](docs/authorization-development.md)。

存储切换阶段的 228/236 项测试和 PG/Redis 探针记录保留在[存储验证](docs/reviews/storage-switch-validation-2026-10-25.md)；随后 P1a 和复查已完成，当前结果见本文开头及[代码架构复查](docs/reviews/code-architecture-review-2026-10-25.md)。业务 Redis 使用方和远端 CI 尚未验收；历史审查保留各自提交、环境和范围。

## 负责保存

- 公司成员、岗位、团队及人员之间的协作关系；独立 Agent 组织关系属于后置候选。
- 目标、任务、承诺、依赖、认领、阻塞和交付状态。
- 授权、审批、事件、运行、访问和审计记录。
- 经营看板、资源瓶颈、风险和待决事项。
- ScoreBook 的生效版本与 Maestro 的运行状态。

## 不负责替代

- ScoreBook 的治理与正式知识版本。
- Virtuoso 的私人会话和个人缓存。
- 专业财务、人事、客户或行业系统的领域事实。

Stage 是当前组织协作事实的权威来源；GitLab 实际权限和专业业务事实仍以对应系统为准。看板是 Stage 的投影，不是单独的可写数据库。
