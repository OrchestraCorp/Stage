# Stage

Stage 是 OrchestraCorp 的可信组织协作运行底座，用“舞台 / 演出台”指代组织正在发生的工作。OrchestraCorp 的长期愿景是 AI 驱动、安全可靠的分布式工作协作平台。

已确定采用自研组件运行时和自研业务流程运行器，参考 Cordis 的组件思想，由 Stage 自己定义一致性工作规范。Graphon / Dify 仅作为研究参考；愿景、职责及演进边界见[自研运行时方向](docs/vision-and-runtime-direction.md)。

产品源码由 GitHub 私有仓库 `OrchestraCorp/Stage` 管理；企业 GitLab 连接器和企业仓库权限仍按业务架构单独建设。

后续实现的统一入口见 [docs 设计目录](docs/README.md)：一期优先建设类Cordis、真实身份验证和WASM共同基座。最新[核对记录](docs/design-review-2026-10-24.md)、[核心对象](docs/core-object-contracts.md)、[身份规范](docs/identity-and-authorization.md)、[WASM宿主](docs/wasm-host-design.md)、[目标架构](docs/architecture.md)、[一期验收](docs/phase-1.md)和[长期路线](docs/roadmap.md)形成统一设计入口。原[完整任务业务](docs/business-slice-plan.md)后置保留。当前仅持久命令/审计/outbox/delivery内部链已实现，其他新基座仍待实现。

[组织运行与热协作架构初稿](architecture.md)从权威边界、六个能力域、核心对象、任务闭环、Agent 执行、GitLab 接入和首版切片展开设计。当前包含设计文档和可测试的工程底板，尚未连接真实身份系统、GitLab 或企业运行环境。

[z-pool 参考实现分析](reference-z-pool.md)记录了从现有后端实现中吸收的工程方法：单一真源、权限点、状态拆分、原子承接、幂等、审计、outbox、未知结果和契约测试。它不把 z-pool 的供应商资源池业务直接复制到 Stage。

[可组合后端框架设计](composable-runtime-design.md)保留早期上下文、能力决策与组合提案；新的[目标架构](docs/architecture.md)将组件 `ComponentContext`、每请求 `ExecutionContext` 及授权输入分开，并区分运行资源清理与持久业务事务。两份文档均不表示新运行时已实现。

[OA 能力组合策略](composition-strategy.md)明确采用“预建能力模块 + 声明式编排 + 受控扩展”：Maestro 负责理解和提出组合蓝图，Stage 负责验证、执行和留痕，首版不加载 Agent 生成的任意后端代码。

[RBAC、ABAC 与 ReBAC 授权模型](authorization-model-design.md)说明三种授权模型如何分工，并给出沿用 z-pool-v2 权限代码、审计、幂等、outbox 和契约测试习惯的迁移路线。

## 工程底板

本仓库已初始化第一版 Python/uv 工程骨架，沿用 z-pool-v2 熟悉的 `app/core`、`app/authz`、`app/models`、`app/features`、`contracts`、`migrations`、`scripts` 和 `tests` 分层。当前已提供：

- FastAPI 应用入口与 `/health`。
- RBAC 权限点和显式并集。
- 有效时间的 ReBAC 关系元组。
- 分类、对象版本、主体/委托状态等 ABAC 固定谓词。
- 可解释的授权决策结构。
- SQLAlchemy 异步数据库、模型和 Alembic 入口。
- MySQL 和 PostgreSQL 异步驱动；SQLite 作为本地迁移/测试选项。
- UTC 时间类型、精确标识符比较和前向迁移预检。
- 应用生命周期、请求 ID 和脱敏错误响应。
- 持久化命令执行、tenant guard、幂等结果、事务审计和 outbox 投递 worker 的第一版内部实现；尚未接入 HTTP、真实身份 adapter、WASM runner 或生产 sink。

底板尚未连接真实身份系统、GitLab、Stage 数据库或企业配置。`uv.lock` 已提交，依赖使用 Python 3.12 和 `uv sync --locked` 固定。

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

数据库 URL 支持三种异步驱动：`mysql+aiomysql`、`postgresql+asyncpg` 和 `sqlite+aiosqlite`。MySQL 是当前默认生产配置；选择 PostgreSQL 后需要执行同样的迁移和存储语义验收，不能把更换 URL 当作完整兼容证明。底板不同时连接或同步两个生产数据库。

应用存储层统一 UTC aware 时间和精确标识符比较；MySQL 与 PostgreSQL 会话显式使用 UTC、READ COMMITTED。当前验证基线为 MySQL 8.4、PostgreSQL 17；SQLite 仅用于开发测试，不证明生产并发正确性。真实两库的迁移循环与存储测试已加入 GitLab CI；尚未在本地真实服务或远端流水线执行。

生产环境必须显式设置 `STAGE_DATABASE_URL`，不会自动采用默认连接，也不允许 SQLite；生产 `/docs`、`/openapi.json` 默认关闭。配置对象的 repr、普通序列化及校验错误文本隐藏数据库 URL；不要记录配置的原始输入或底层异常对象。`/health` 始终仅为 liveness，不表示数据库已可用。

授权内核当前只接受内置策略版本 `stage-authz:builtin-v1`。任职、关系、资源属性和委托必须由服务端从权威记录构造，不能将请求 body 当成授权上下文；真实身份认证、上下文加载器、动态策略发布和业务写 API 仍待实现。

本轮审查结果、已修问题与后续边界见[框架审查记录](docs/framework-review-2026-10-19.md)。

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
