# Stage

Stage 是 OrchestraCorp 的可信组织协作运行底座，用“舞台 / 演出台”指代组织正在发生的工作。OrchestraCorp 的长期愿景是 AI 驱动、安全可靠的分布式工作协作平台。

已确定采用自研组件运行时和自研业务流程运行器，参考 Cordis 的组件思想，由 Stage 自己定义一致性工作规范。Graphon / Dify 仅作为研究参考；愿景、职责及演进边界见[自研运行时方向](docs/vision-and-runtime-direction.md)。

2026-10-25 已完成[一期框架实施规格](docs/framework-foundation.md)的 P1a.1～P1a.3：Core、生命周期/事件、PostgreSQL/Redis provider 与 FastAPI 装配。34 个 Core 场景与 12 个边界场景的映射验收为 46/46，真实服务全套 324 项测试通过；复查与只读冒烟见[代码架构复查](docs/reviews/code-architecture-review-2026-10-25.md)。云设备服务已运行，见[运行说明](deployment/dev/README.md)。

产品源码由 GitHub 私有仓库 `OrchestraCorp/Stage` 管理；企业 GitLab 连接器和企业仓库权限仍按业务架构单独建设。

后续实现的统一入口见 [docs 设计目录](docs/README.md)：一期优先建设类 Cordis、真实身份验证和 WASM 共同基座。[核心对象](docs/core-object-contracts.md)、[身份规范](docs/identity-and-authorization.md)、[WASM 宿主](docs/wasm-host-design.md)和[一期验收](docs/phase-1.md)定义阶段边界。当前 P1a Core、存储 provider、系统探针及内部命令/投递组件已实现；下一阶段从 [P1b 实施入口](docs/p1b-implementation-entry.md)推进真实身份、权限与鉴权。完整任务业务仍后置。

2026-10-25 确定长期使用 **PostgreSQL 17＋Redis 8**。PostgreSQL 是唯一主库；Redis 只承担可重建缓存、非权威短期协调和可丢失唤醒。当前授权、幂等、outbox、业务租约及提交结果仍以 PostgreSQL 为准，完整边界见[存储策略](docs/storage-strategy.md)。

[组织运行与热协作架构初稿](architecture.md)从权威边界、六个能力域、核心对象、任务闭环、Agent 执行、GitLab 接入和首版切片展开设计。当前包含设计文档和可测试的工程底板，尚未连接真实身份系统、GitLab 或企业运行环境。

[z-pool 参考实现分析](reference-z-pool.md)记录了从现有后端实现中吸收的工程方法：单一真源、权限点、状态拆分、原子承接、幂等、审计、outbox、未知结果和契约测试。它不把 z-pool 的供应商资源池业务直接复制到 Stage。

[可组合后端框架设计](composable-runtime-design.md)保留早期提案；新的[目标架构](docs/architecture.md)将组件 `ComponentContext`、每请求 `ExecutionContext` 及授权输入分开，并区分运行资源清理与持久业务事务。设计文档不单独构成验收，P1a 实际完成范围以[验证记录](docs/reviews/p1a-validation-2026-10-25.md)为准。

[OA 能力组合策略](composition-strategy.md)明确采用“预建能力模块 + 声明式编排 + 受控扩展”：Maestro 负责理解和提出组合蓝图，Stage 负责验证、执行和留痕，首版不加载 Agent 生成的任意后端代码。

[RBAC、ABAC 与 ReBAC 授权模型](authorization-model-design.md)说明三种授权模型如何分工，并给出沿用 z-pool-v2 权限代码、审计、幂等、outbox 和契约测试习惯的迁移路线。

## 工程底板

本仓库已初始化第一版 Python/uv 工程骨架，沿用 z-pool-v2 熟悉的 `app/core`、`app/authz`、`app/models`、`app/features`、`contracts`、`migrations`、`scripts` 和 `tests` 分层。当前已提供：

- FastAPI 应用入口、独立 Core、`/health` 与受管依赖 `/ready`。
- RBAC 权限点和显式并集。
- 有效时间的 ReBAC 关系元组。
- 分类、对象版本、主体/委托状态等 ABAC 固定谓词。
- 可解释的授权决策结构。
- SQLAlchemy 异步数据库、模型和 Alembic 入口。
- PostgreSQL 异步数据库入口；SQLite 作为本地迁移/快速测试选项，MySQL 退出受支持配置。
- UTC 时间类型、精确标识符比较和前向迁移预检。
- 应用生命周期、请求 ID 和脱敏错误响应。
- 持久化命令执行、tenant guard、幂等结果、事务审计和 outbox 投递 worker；可通过可信组件装配接入 Core，尚未开放业务 HTTP、真实身份 adapter、WASM runner 或生产 sink。

开发设备已连接 PostgreSQL/Redis；真实身份、GitLab 和企业配置尚待接入。依赖使用 Python 3.12 和 `uv sync --locked` 固定。

## 本地检查

```bash
uv sync --locked
uv run pytest
uv run ruff check app tests scripts
uv run ruff format --check app tests scripts
uv run mypy app tests scripts
uv run python scripts/check_contract.py
```

当前健康检查不需要数据库；真正启动数据库相关功能前，设置 `STAGE_DATABASE_URL` 并完成第一版迁移。

生产数据库使用 `postgresql+asyncpg`，开发快测可用 `sqlite+aiosqlite`。MySQL 历史迁移分支保留，不再代表受支持配置。PostgreSQL 和 Redis 的业务边界不能靠双写原子承诺连接：业务、命令结果、审计与 outbox 先在 PostgreSQL 同事务提交，再发送可恢复的失效通知或唤醒。

应用存储层统一 UTC aware 时间和精确标识符比较，PostgreSQL 会话显式使用 UTC、READ COMMITTED。迁移、锁、撤权竞争和幂等正式门禁以 PostgreSQL 17 真库为准；SQLite 不证明生产并发正确性。Redis 缓存必须隔离 tenant、核验当前授权版本并定义超时/不可用策略，清空 Redis 不得丢业务事实。

根目录 `compose.yaml` 的开发服务约定为 PostgreSQL 17（`127.0.0.1:15432`）和 Redis 8（`127.0.0.1:16379`），从 `.env.example` 准备本地配置。Core 装配 PG provider；显式配置 Redis URL 后装配 Redis provider，默认不作为整体就绪的必需项，`STAGE_REDIS_REQUIRED=true` 可将其设为必需。缓存/队列业务仍需各自集成；`/health` 始终只检查进程存活。

本地服务启动及 Redis 探针示例（仅虚构开发凭据）：

```bash
docker compose up -d
STAGE_DATABASE_URL=postgresql+asyncpg://stage:stage-dev-postgres@127.0.0.1:15432/stage uv run alembic upgrade head
TEST_REDIS_URL=redis://:stage-dev-redis@127.0.0.1:16379/0 uv run python scripts/check_redis.py
```

Redis 开发服务关闭持久化，内存上限 256 MiB，采用 `noeviction`；容量满时的写失败须由使用方处理。探针只操作随机 key，不执行 FLUSH。GitHub Actions 为主 CI，GitLab CI 保留镜像模板；本轮 PostgreSQL/Redis 目标取代历史双库门禁，不据配置文件宣称远端执行通过。

生产环境必须显式设置 `STAGE_DATABASE_URL`，不会自动采用默认连接，也不允许 SQLite；生产 `/docs`、`/openapi.json` 默认关闭。配置对象的 repr、普通序列化及校验错误文本隐藏数据库 URL；不要记录配置的原始输入或底层异常对象。`/health` 始终仅为 liveness，不表示数据库已可用。

授权内核当前只接受内置策略版本 `stage-authz:builtin-v1`。任职、关系、资源属性和委托必须由服务端从权威记录构造，不能将请求 body 当成授权上下文；真实身份认证、上下文加载器、动态策略发布和业务写 API 仍待实现。

存储切换阶段的 228/236 项测试和 PG/Redis 探针记录保留在[存储验证](docs/reviews/storage-switch-validation-2026-10-25.md)；随后 P1a 和复查已完成，当前结果见本文开头及[代码架构复查](docs/reviews/code-architecture-review-2026-10-25.md)。业务 Redis 使用方和远端 CI 尚未验收；历史审查保留各自提交、环境和范围。

## 负责保存

- 组织、岗位、团队和 Agent 当前关系。
- 目标、任务、承诺、依赖、认领、阻塞和交付状态。
- 授权、审批、事件、运行、访问和审计记录。
- 经营看板、资源瓶颈、风险和待决事项。
- ScoreBook 的生效版本与 Maestro 的运行状态。

## 不负责替代

- ScoreBook 的治理与正式知识版本。
- Virtuoso 的私人会话和个人缓存。
- 专业财务、人事、客户或行业系统的领域事实。

Stage 是当前组织协作事实的权威来源；GitLab 实际权限和专业业务事实仍以对应系统为准。看板是 Stage 的投影，不是单独的可写数据库。
