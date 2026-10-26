# Stage live-service smoke tests

The suite is opt-in and read-only. A regular `uv run pytest` run skips every
live check unless its corresponding `STAGE_SMOKE_*` target is explicitly set.
The HTTP checks use only `GET /health`, `GET /ready`, and `GET /openapi.json`.
The direct dependency checks execute `SELECT 1` in PostgreSQL and `PING` in
Redis. They never reset a database, issue `FLUSH*`, or write a Redis key.

Run the cloud development service checks with:

```bash
STAGE_SMOKE_BASE_URL=http://127.0.0.1:8080 \
STAGE_SMOKE_DATABASE_URL="$STAGE_DATABASE_URL" \
STAGE_SMOKE_REDIS_URL="$STAGE_REDIS_URL" \
uv run python scripts/smoke.py
```

Use the root-only device environment file when running that command on the
development device. The CLI options (`--base-url`, `--database-url`, and
`--redis-url`) are convenient local overrides; environment variables keep
credentials out of the process argument list. The CLI options map to these
test variables:

| Variable | Purpose | Default |
| --- | --- | --- |
| `STAGE_SMOKE_BASE_URL` | Target Stage HTTP service | unset, HTTP checks skipped |
| `STAGE_SMOKE_DATABASE_URL` | Direct database probe target | unset, probe skipped |
| `STAGE_SMOKE_REDIS_URL` | Direct Redis probe target | unset, probe skipped |
| `STAGE_SMOKE_EXPECT_READY` | Require `/ready` 200 (`0` expects 503) | `1` |
| `STAGE_SMOKE_EXPECT_REDIS` | Expected `/ready` Redis state | unset; `ready` when a smoke Redis URL is set |
| `STAGE_SMOKE_EXPECT_OPENAPI` | `auto`, `1`, or `0` for `/openapi.json` | `auto` |
| `STAGE_SMOKE_CONCURRENCY` | Requests per endpoint in the concurrency check | `8` |
| `STAGE_SMOKE_ROUNDS` | Number of concurrency rounds | `2` |
| `STAGE_SMOKE_TIMEOUT_SECONDS` | HTTP/direct probe timeout | `5` |

The suite rejects configured connection material in live response bodies and
does not include those values in assertion messages.

The separate [real human work integration test](../integration/test_human_work_live.py)
performs authenticated writes through a temporary loopback Stage and a real
local Keycloak. It requires an explicitly disposable PostgreSQL test database,
reset authorization, and an operator-owned 0600 credentials file. It migrates
and resets that test database and must run separately from other tests sharing
the same database. See the [development runbook](../../deployment/dev/README.md#独立测试库的真实人员写入验收)
for the opt-in command and scope. These live-service smoke checks remain read-only.
