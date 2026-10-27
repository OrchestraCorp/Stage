<p align="center"><img src="assets/brand/stage.png" width="144" height="144" alt="Stage logo"></p>

# Stage

English | [中文](README.zh.md)

**A collaboration backend for self-hosted enterprise deployments.**

Stage provides identity, organization permissions, controlled operations and audit for OrchestraCorp. People select an explicit role assignment through a client, and agents use the same APIs under human supervision. The server checks current identity, permissions and object state before allowing an operation, then persists its outcome.

The project is available under the [MIT license](LICENSE) and is in developer preview. A working backend foundation and the first administration feature set are available; complete enterprise workflows, client integration and production operations acceptance remain in progress.

[Local development](#local-development) · [API contract](contracts/openapi.yaml) · [Design documentation](docs/README.md) · [Roadmap](docs/roadmap.md)

## The OrchestraCorp projects

| Project | Responsibility |
|---|---|
| **[Stage](https://github.com/OrchestraCorp/Stage)** | Organization identity, permissions, business operations, persistent results and audit. |
| [Maestro](https://github.com/OrchestraCorp/Maestro) | Enterprise administration and coordination, with people supervising agents that organize knowledge and assist with work. |
| [Virtuoso](https://github.com/OrchestraCorp/Virtuoso) | The employee workspace for conversations, tools and everyday work with agents. |
| [ScoreBook](https://github.com/OrchestraCorp/ScoreBook) | Versioned organizational handbooks, role contracts, formal knowledge and process templates. |

The projects collaborate through explicit interfaces, with integrations delivered incrementally. Maestro hosts the administration experience; Stage stores authoritative server-side facts and enforces permissions. Client model credentials, private conversations and formal enterprise records are managed separately.

## Available capabilities

- **Human authentication and account security:** native passwords and TOTP, factor enrollment and recovery, self-service password changes, session management and application consent management.
- **Unified login for registered applications:** OAuth/OIDC authorization code flow with PKCE, multiple registered clients, SSO, refresh rotation, token revocation and confirmed logout. See the [OAuth capability matrix](docs/oauth-functional-coverage.md) for the supported scope.
- **Current identity and role assignments:** self-discovery of identity and assignments, pinned role revisions, authorization scopes, direct relationships, and time, state and version conditions. Each operation selects one explicit assignment and rechecks authorization.
- **Initial administration features:** first-administrator nomination and personal acceptance with recent MFA, an existing-member directory, bounded grants and revocations of ordinary role assignments, administration audit, and recovery using the original operation key.
- **Minimal work-item operations:** read an existing work item, edit its title and description subject to authorization and version checks, and recover the result using the original idempotency key.
- **Backend foundation:** component dependencies and lifecycle management, database migrations, short transactions, idempotency, audit, a durable delivery foundation, and health and readiness probes.

Employee activation, coordinated offboarding, multiple administrators, complete task and approval workflows, durable workflows and controlled WASM execution follow the [roadmap](docs/roadmap.md). The delivery foundation does not yet establish production Worker integration, and server-side verification does not replace the actual Maestro administration interface or user acceptance.

## Architecture and deployment boundaries

Stage is a modular monolith. FastAPI handles HTTP requests, application providers manage service dependencies, Runtime Core manages component lifecycles, domain services implement business rules, and PostgreSQL stores authoritative facts.

| Layer | Current choice |
|---|---|
| Language and interfaces | Python 3.12+, FastAPI, Pydantic and versioned `/api/v1` endpoints; standard OAuth endpoints are separate. |
| Persistent storage | PostgreSQL 17, SQLAlchemy's async ORM and Alembic migrations. |
| Supporting infrastructure | Redis 8; it is not authoritative for authorization, idempotency or business outcomes. |
| Transactions and permissions | Current human identity checks, organization read/write coordination, object versions, idempotency and atomic audit. |
| Component runtime | An independently developed Runtime Core informed by Cordis dependency and resource-lifecycle mechanisms. |

Each Stage deployment has one organization authorization root, a dedicated database and a fixed environment. The root defines authorization and governance boundaries; departments, projects and legal entities are modeled as business needs arise. Clients may connect to separate Stage deployments, with credentials and working context isolated per instance. See the [multi-instance connection contract](docs/maestro-multi-stage-connections.md).

The purpose of studying Cordis is to support composition, shutdown and recovery of real backend components. Stage owns its identity, transaction and persistent-fact protocols. See the [current architecture](docs/current-code-architecture.md) and [runtime evolution](docs/runtime-evolution-and-composition.md).

<a id="本地开发"></a>

## Local development

Use Python 3.12+, [uv](https://docs.astral.sh/uv/) and Docker Compose, or provide PostgreSQL 17 and Redis 8 yourself.

The following steps target a fresh local development checkout. Credentials in `compose.yaml` and `.env.example` are public local-development examples.

```bash
git clone https://github.com/OrchestraCorp/Stage.git
cd Stage
uv sync --locked --dev
cp .env.example .env
docker compose up -d --wait
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Once started, these endpoints are available:

- `http://127.0.0.1:8080/health`: process liveness.
- `http://127.0.0.1:8080/ready`: required components, database and migration readiness.
- `http://127.0.0.1:8080/docs`: development API documentation.

These steps start the service foundation. Organizations, people and permissions require explicit configuration; migrations do not automatically create administrators. Native authentication is disabled by default and requires registered identities, private keys and MFA configuration. Continue with the [identity development guide](deployment/identity/README.md), [native authentication runbook](deployment/identity/native-authentication.md) and [first-administrator guide](deployment/identity/human-administration.md). The development installation tools do not constitute a complete production initialization and recovery process.

Production deployment requires dedicated credentials, HTTPS, backup and recovery, and clearly assigned identity-management responsibilities. Refer to the relevant runbooks for specific limitations.

## Checks and verification

```bash
uv run ruff check app tests scripts
uv run ruff format --check app tests scripts
uv run mypy app tests scripts
uv run python scripts/check_contract.py --check
uv run pytest -q
```

Without an explicit test database, the default suite uses SQLite for fast tests. PostgreSQL is the runtime database; authorization races, revocation ordering and transaction consistency require real PostgreSQL verification. Database tests reset test tables, so use a separate, explicitly authorized test database. See the [storage policy](docs/storage-strategy.md) and [verification guide](deployment/dev/release-freeze.md).

The latest [administration acceptance report](docs/reviews/human-administration-first-slice-2026-10-26.md) records the scope and results of PostgreSQL, SQLite, Chromium, static checks and live smoke tests. Historical results do not automatically establish that a newer version passes.

## Documentation and contribution

Most linked design documents and runbooks are currently in Chinese.

- [Design and implementation index](docs/README.md): architecture, identity, storage and the phased roadmap.
- [Administration APIs and Maestro integration](deployment/identity/human-administration.md): the first administrator, assignments, audit and error recovery.
- [Client login and work-item contract](docs/maestro-stage-p1b-handoff.md): authentication, assignment selection, version conflicts and uncertain results.
- [Native OAuth/OIDC](docs/oauth-functional-coverage.md): supported and unsupported protocol capabilities.
- [Development environment](deployment/dev/README.md): records for this project's development setup.
- [Maintainer instructions](AGENT.md): identity, authorization, transaction and sensitive-data boundaries.

Use [Issues](https://github.com/OrchestraCorp/Stage/issues) to discuss requirements or report problems. API changes must update OpenAPI and contract tests; authorization or transaction changes should include negative and concurrency verification. Do not commit real enterprise data, keys or runtime credentials.

## License

[MIT](LICENSE). Commercial use, modification and distribution are permitted, subject to retaining the copyright and license notices. Self-hosted deployments, customer data and private configuration do not have to be made public by adopting this project. Third-party dependencies retain their respective licenses.
