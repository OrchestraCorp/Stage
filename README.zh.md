<p align="center"><img src="assets/brand/stage.png" width="144" height="144" alt="Stage logo"></p>

# Stage

[English](README.md) | 中文

**面向企业自托管的组织协作后端。**

Stage 为 OrchestraCorp 提供身份认证、组织权限、受控操作和审计。人员通过客户端选择明确任职，Agent 在人员监督下调用同一套接口；服务端根据当前身份、权限和对象状态决定操作是否可以执行，并持久保存结果。

本项目采用 [MIT 许可证](LICENSE)，处于开发者预览阶段。当前已有可运行的后端基础和首个管理切片，完整企业业务流程、客户端联调与生产运维验收仍在推进。

[开发启动](#本地开发) · [API 合同](contracts/openapi.yaml) · [设计文档](docs/README.md) · [开发路线](docs/roadmap.md)

## OrchestraCorp 的四个项目

| 项目 | 负责什么 |
|---|---|
| **[Stage](https://github.com/OrchestraCorp/Stage)** | 组织身份、权限、业务操作、持久结果与审计。 |
| [Maestro](https://github.com/OrchestraCorp/Maestro) | 企业管理与协作工作台，支持人员监督 Agent 整理知识、协调工作。 |
| [Virtuoso](https://github.com/OrchestraCorp/Virtuoso) | 员工使用 Agent、会话和工具完成日常工作的个人工作台。 |
| [ScoreBook](https://github.com/OrchestraCorp/ScoreBook) | 组织手册、岗位契约、正式知识与流程模板的版本化载体。 |

这些项目通过明确的接口协作，集成能力逐步交付。Maestro 承载管理界面；Stage 保存服务端事实并执行权限。客户端的模型连接凭据、私人会话与企业正式记录分别管理。

## 当前可以做什么

- **人员认证与账户安全**：原生密码＋TOTP、因素绑定与恢复、本人改密、会话和应用同意管理。
- **受控应用统一登录**：OAuth/OIDC 授权码＋PKCE、多客户端登记、SSO、刷新轮换、令牌撤销与确认退出。支持范围见 [OAuth 功能矩阵](docs/oauth-functional-coverage.md)。
- **当前身份与任职**：本人身份和任职发现、固定角色版本、授权范围、直接关系和时间／状态／版本条件。每次操作选择一个明确任职并重新授权。
- **首个管理切片**：首管提名与本人近期 MFA 接受、现有人员目录、有限普通任职授予／撤销、本管理域审计和原键恢复。
- **最小工作项操作**：读取已有工作项，按版本与权限修改标题和说明，使用原幂等键恢复提交结果。
- **后端基础**：组件依赖和生命周期管理、数据库迁移、短事务、幂等、审计、可靠投递底板、健康与就绪探针。

员工开户激活、组合离职、多管理员、完整任务／审批、持久工作流和受控 WASM 等能力按[路线](docs/roadmap.md)继续建设。现有投递底板不等于已接通生产 Worker；服务端验证也不代替 Maestro 的实际管理界面与用户验收。

## 架构与部署边界

Stage 是组件化的模块单体：FastAPI 提供 HTTP 接入，应用 provider 管理服务依赖，Runtime Core 管组件生命周期，领域服务负责业务规则，PostgreSQL 保存权威事实。

| 层次 | 当前选择 |
|---|---|
| 语言与接口 | Python 3.12+、FastAPI、Pydantic、版本化 `/api/v1`；OAuth 标准入口独立。 |
| 持久存储 | PostgreSQL 17、SQLAlchemy 异步 ORM、Alembic 迁移。 |
| 辅助设施 | Redis 8；当前不作为授权、幂等或业务结果的权威。 |
| 事务与权限 | 当前人员重验、组织读写保护、对象版本、幂等与原子审计。 |
| 组件运行 | 自研 Runtime Core，参考 Cordis 的依赖与资源生命周期机制。 |

每套 Stage 使用一个组织授权根、一套专属数据库和固定环境。组织根表示授权和治理边界；部门、项目及法人关系按业务需要建模。客户端可以分别连接不同 Stage，凭据与工作上下文按实例隔离，详见[多实例连接合同](docs/maestro-multi-stage-connections.md)。

参考 Cordis 的目标是支持实际后端组件的组合、停止和恢复。Stage 的身份、事务与持久事实协议仍由自身负责；详见[当前架构图](docs/current-code-architecture.md)和[运行时演进](docs/runtime-evolution-and-composition.md)。

## 本地开发

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/) 和 Docker Compose，或自行准备 PostgreSQL 17／Redis 8。

以下步骤面向新的本地开发目录；`compose.yaml` 与 `.env.example` 中的凭据是公开的本地示例。

```bash
git clone https://github.com/OrchestraCorp/Stage.git
cd Stage
uv sync --locked --dev
cp .env.example .env
docker compose up -d --wait
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8080
```

启动后可访问：

- `http://127.0.0.1:8080/health`：进程健康。
- `http://127.0.0.1:8080/ready`：必需组件、数据库和迁移就绪。
- `http://127.0.0.1:8080/docs`：开发环境 API 文档。

这条路径启动服务基础；组织、人员和权限需要显式配置，迁移不会自动创建管理员。原生认证默认关闭，启用需登记身份、配置私有密钥与 MFA。后续按[身份开发说明](deployment/identity/README.md)、[原生认证手册](deployment/identity/native-authentication.md)和[首管引导手册](deployment/identity/human-administration.md)操作。当前开发安装工具不代表完整生产初始化与恢复流程。

生产部署需要独立凭据、HTTPS、备份恢复和明确的身份管理责任；具体限制以对应手册为准。

## 检查与验证

```bash
uv run ruff check app tests scripts
uv run ruff format --check app tests scripts
uv run mypy app tests scripts
uv run python scripts/check_contract.py --check
uv run pytest -q
```

默认未设置测试数据库时使用 SQLite 快测。运行期间的主库为 PostgreSQL；授权竞争、撤销排序和事务一致性必须通过真实 PostgreSQL 验证。数据库测试会重置测试表，须使用明确授权的独立测试库，见[存储说明](docs/storage-strategy.md)和[验证手册](deployment/dev/release-freeze.md)。

最近的[管理切片验收](docs/reviews/human-administration-first-slice-2026-10-26.md)记录了 PostgreSQL、SQLite、Chromium、静态检查和在线冒烟的具体范围与结果；历史数字不作为新版本自动通过的证明。

## 文档与贡献

- [设计与实施入口](docs/README.md)：架构、身份、存储和阶段路线。
- [管理 API 与 Maestro 联调](deployment/identity/human-administration.md)：首管、任职、审计及错误恢复。
- [客户端登录与工作项合同](docs/maestro-stage-p1b-handoff.md)：认证、任职选择、版本冲突和未知结果。
- [原生 OAuth/OIDC](docs/oauth-functional-coverage.md)：已支持与尚未支持的协议能力。
- [开发设备说明](deployment/dev/README.md)：本项目开发环境记录。
- [维护约束](AGENT.md)：身份、权限、事务与敏感数据边界。

欢迎通过 [Issues](https://github.com/OrchestraCorp/Stage/issues)讨论需求或报告问题。修改 API 时同步 OpenAPI 与契约测试；涉及权限或事务的改动应提供负向和并发验证。不要提交实际企业资料、密钥或运行凭据。

## 许可证

[MIT](LICENSE)。允许商业使用、修改和分发，须保留版权及许可声明。企业自托管实例、客户数据和私有配置不因采用本项目而需要公开；第三方依赖保留各自许可证。
