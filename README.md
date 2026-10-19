# Stage

Stage 是 OrchestraCorp 的核心同步与热协作系统，用“舞台 / 演出台”指代组织正在发生的工作。

[组织运行与热协作架构初稿](architecture.md)从权威边界、六个能力域、核心对象、任务闭环、Agent 执行、GitLab 接入和首版切片展开设计。当前只有设计文档，尚未实现数据库、API 或运行服务。

[z-pool 参考实现分析](reference-z-pool.md)记录了从现有后端实现中吸收的工程方法：单一真源、权限点、状态拆分、原子承接、幂等、审计、outbox、未知结果和契约测试。它不把 z-pool 的供应商资源池业务直接复制到 Stage。

[可组合后端框架设计](composable-runtime-design.md)进一步把权限、业务、时间和空间统一到 `RuntimeContext`、能力决策、业务命令、依赖解析和 effect/compensation 记录中；这是首版框架提案，尚未实现。

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
- 审计、幂等和 outbox 的底层结构。

底板尚未连接真实身份系统、GitLab、Stage 数据库或企业配置。依赖锁文件需要在安装 `uv` 的 Python 3.12 环境中生成后再提交。

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
