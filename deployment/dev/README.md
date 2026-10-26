# 当前云开发设备

最新开发服务已备份并迁移至 `a7d4e2c9f501`（38张应用表），运行首个人员管理切片；`/health`、`/ready`及原生认证在线冒烟通过，见[本轮验收](../../docs/reviews/human-administration-first-slice-2026-10-26.md)。新增管理表为空，管理员须按[首管手册](../identity/human-administration.md)明确提名并本人接受。以下较早版本、Keycloak fixture和P1b.3/.4计数保留对应轮次部署记录，不能覆盖本段当前状态。

2026-10-26 已完成本人任职发现及密码＋TOTP 登录 MFA。常驻 `stage-development` 的 human policy 为 required/version 2，最大认证年龄 900 秒；原 issuer/client/人员 subject 保留，原 service client 仍独立。Stage schema head 为 `9c2e7a4b6d10`（26 张应用表）。本次已经显式启用，其他部署升级迁移默认 disabled，不会自动开启。实施与回退材料见[P1b.4 记录](../../docs/reviews/p1b4-assignments-mfa-2026-10-26.md)。

运行模型为企业私有化部署：**一套 Stage、一个组织授权根、一套数据库、一个环境**；授权根是信任与治理边界，不等同于一个法人公司。客户端不选择组织，部门/项目以后按需用组织单元和 scope 管理。当前公开身份 API 为 v1，见[组织授权根语义](../../docs/organization-root-semantics.md)和[部署与 API 合同](../../docs/private-deployment-api-v1.md)。

本设备运行单 ASGI worker 的 Stage，源码路径 `/root/OrchestraCorp/Stage`。本轮工作区增加 P1b.3 人员工作项 provider，重启后的实际验收以[P1b.3 记录](../../docs/reviews/p1b3-human-work-2026-10-25.md)为准。Stage/PostgreSQL/Redis 以 `orchstage` 非登录用户运行，隔离测试 IdP 以 `orchidp` 运行；四个 systemd 服务均启用开机启动和失败重启：

| 服务 | 监听 | 用途 |
|---|---|---|
| `orchestra-stage-dev` | `127.0.0.1:8080` | Stage 探针、v1 身份/人员工作项接口、开发 OpenAPI；实际重启验收见本轮记录 |
| `orchestra-stage-postgres-dev` | `127.0.0.1:15432` | PostgreSQL 17.11，开发库 `stage` |
| `orchestra-stage-redis-dev` | `127.0.0.1:16379` | Redis 8.2.3，缓存/辅助基础设施 |
| `orchestra-stage-idp-dev` | `127.0.0.1:18080` | Keycloak 26.4.7 + OpenJDK 21，隔离真实协议测试 IdP |

```bash
systemctl status orchestra-stage-dev orchestra-stage-postgres-dev orchestra-stage-redis-dev orchestra-stage-idp-dev
curl --fail http://127.0.0.1:8080/health
curl --fail http://127.0.0.1:8080/ready
systemctl restart orchestra-stage-dev
journalctl -u orchestra-stage-dev -n 80 --no-pager
```

修改代码后重启 Stage。当前 systemd 单元通过只读 bind mount 将工作区映射到 `/srv/orchestra-stage`，运行用户不获得 `/root` 全目录访问。单元模板见 [orchestra-stage-dev.service](orchestra-stage-dev.service)。PostgreSQL/Redis 二进制位于 `/opt/orchestracorp/stage-dev`，数据目录为 `/var/lib/orchestracorp-stage-dev`；来源沿用存储验证时核验的官方 PGDG 包与 Redis 源码，没有安装 Docker daemon。

连接配置位于设备专用的 `/etc/orchestra-stage-dev.env`（root 0600），使用本地生成密码，不提交仓库。只在受信任终端加载它：

```bash
set -a
. /etc/orchestra-stage-dev.env
set +a
cd /root/OrchestraCorp/Stage
.venv/bin/python -m alembic upgrade head
```

迁移是独立部署操作，Core 启动仅核验 schema，不自动迁移或创建业务表。测试使用另一个 `stage_test_core` 库及显式重置授权，不能把 `stage` 开发库传给重置测试。Docker 可用的其他设备继续使用仓库 `compose.yaml`，不需复制此机数据或凭据。

`/health` 只报告进程存活；PG/schema 或必需组件不可用使 `/ready` 返回 503，Redis 默认可选。默认装配包括身份与人员工作项服务，ready 不证明组织成员、任职或资源已登记，也不会自动授予权限。元数据入口每次验证真实人员凭据与明确任职；通用命令/投递适配仍由可信组合提供 loader、authorizer 和 sink，生产投递尚未接入。

`GET /api/v1/identity/me` 要求 Bearer access token，query 仅为 `profile_ref/profile_version`，返回服务端确定的组织根和当前成员。新增 `/api/v1/work-items/{work_item_id}` 的读取、`/details` 编辑与 `/operations/{idempotency_key}` 结果查询另要求当前 `assignment_ref`。这些人员入口不接受客户端组织选择或 service token，不把身份读取响应当作权限票据。接口细则见[授权开发操作](../../docs/authorization-development.md)。

身份与工作项路径的全部响应统一设置 `Cache-Control: no-store`，包括框架生成的 422、404/405 与通用 500。身份预算由 `STAGE_IDENTITY_REQUEST_TIMEOUT_SECONDS` 控制；人员命令另由 `STAGE_COMMAND_REQUEST_TIMEOUT_SECONDS` 限制全程，默认均 10 秒、允许大于 0 且不超过 60 秒。预算包含依赖获取、认证和数据库锁/查询等待；预算到期返回 503，认证证据到期返回 401，均不延长身份有效期。提交已开始后的不确定结果沿原幂等键恢复。

## 独立测试库的真实人员写入验收

[真实人员工作项集成测试](../../tests/integration/test_human_work_live.py)使用已配置的隔离 Keycloak，但自行启动临时 loopback Stage 监听；不向常驻开发服务或 `stage` 开发库安装角色和工作项。它在显式可丢弃 PostgreSQL 测试库运行 Alembic head、身份 bootstrap、有限授权安装与合成 WorkItem，结束后清理测试表和 Alembic 版本表。

先在受信任终端将 `TEST_DATABASE_URL` 设为独立的 `stage_test*` 或 `stage_ci*` 数据库；不要从常驻服务的 `STAGE_DATABASE_URL` 默认复制。确认该库可以被重置后单独执行，不能与使用同一测试库的其他测试并行：

```bash
STAGE_ALLOW_TEST_DATABASE_RESET=1 \
STAGE_HUMAN_WORK_SMOKE_CREDENTIALS=/root/.local/state/orchestra-stage/identity/credentials.json \
uv run pytest -q tests/integration/test_human_work_live.py
```

凭据文件必须属于执行操作者且权限为 0600；测试只接受本地 IdP/回调。未显式设置测试数据库和凭据路径时跳过，不计通过。它验证真实授权码＋PKCE、HTTP 编辑、token 刷新后原 key 重放、查询结果、版本/输入冲突、身份字段注入、ID token/service token 拒绝及任职撤销后的 403；数据库最终核验单次版本变化、一个命令和审计、零 Outbox。与[只读服务冒烟](../../tests/smoke/README.md)分别运行、分别留证。

本机虚构组织 `stage-development` 已通过受控离线工具登记 human/service 主体和成员，未授予任何角色或业务 grants。私有目录 `/root/.local/state/orchestra-stage/identity` 保存 `credentials.json`、`realm-import.json`、`keycloak.env`、已验证 `metadata.json` 和显式初始化计划 `stage-bootstrap.json`；bootstrap 前开发库备份为 `stage-before-identity-bootstrap.dump`。文件不进入仓库，凭据不粘贴到日志。初始化不是首个登录自动开权；工具仅限 dev/test、本机 OS 操作者、显式数据库配置。

加载上面的本机环境后，幂等初始化命令为：

```bash
uv run python scripts/identity_admin.py --environment dev bootstrap \
  --plan /root/.local/state/orchestra-stage/identity/stage-bootstrap.json \
  --idempotency-key stage-local-identity-v1
```

重复同配置只记录必要的幂等操作结果，不重新开权；变更配置被拒绝。撤销和 `result_unknown` 恢复见[P1b 实施记录](../../docs/reviews/p1b-first-slice-2026-10-25.md)。该备份只证明本轮保存了回退材料，不代表生产恢复演练完成。

新的离线计划使用 `organization_ref` 和 `display_name`。撤销组织根使用 `kind: "organization"`、`target_ref` 指向根；撤销成员使用 `kind: "membership"` 并提供 `organization_ref`。旧 `tenant_ref` 和 `kind: "tenant"` 输入仍被明确规范化接受，冲突的双字段拒绝。新旧输入共用历史 canonical digest 编码（包含可选字段的 null），历史 journal 的 `revoke.tenant` 操作名和 result/before/after JSON 不改写；`revoke.organization` 和 `revoke.tenant` 结果查询恢复同一条记录。CLI 对外结果统一使用 `organization_ref`、`organization_authz_epoch` 和 `kind: "organization"`，这样改名不会破坏已提交初始化/撤销的幂等重放。

一个 Stage 数据库只属于一个组织授权根和一个部署环境。组织根单例约束拒绝第二根；根停用仍保留历史引用，不通过另建根替代。未来多个法人或业务组织单元可以在根下按需建模，但不能把它们解释成第二根。迁移发现多根数据即失败，不删记录或自动合并。离线 bootstrap/revoke 在管理锁内、幂等重放前核验数据库已有初始化环境；第二环境安装、误连其他环境或历史混合环境都拒绝写入，不移动或删除历史数据。只读 result 仍按原环境、操作者与操作键恢复既有结果。

真实 IdP 到 Stage 的冒烟命令：

```bash
uv run python scripts/identity_idp_smoke.py \
  --credentials /root/.local/state/orchestra-stage/identity/credentials.json \
  --stage-url http://127.0.0.1:8080 --organization-ref stage-development
```

`--organization-ref` 只核对响应组织，不传给 Stage 作为选择参数。首条切片的 15 项记录保留为历史结果；更新后的 v1/单组织根验收见[本轮记录](../../docs/private-deployment-api-v1.md)。本机 IdP 的精确 profile、重建方式与凭据边界见[身份开发环境](../identity/README.md)。Maestro 的实际登录 UI、回调与 token 保管仍由客户端联调验收。

本地访问可使用现有 SSH 通道转发 8080；18080 只作本机协议测试，不公开数据库、Redis 或测试 IdP。用户已恢复人员路径开发，当前元数据命令与真实联调范围见[P1b.3](../../docs/reviews/p1b3-human-work-2026-10-25.md)；WASM/签名与生产恢复按各阶段验收。[人员—公司—人员边界](../../docs/people-company-collaboration.md)保持。

## 人类授权开发工具

联调前的身份与权限复查、组织锁基线和后续边界见[P1b 收尾](../../docs/reviews/p1b-closeout-2026-10-25.md)；客户端配置与恢复协议见[Maestro 合同](../../docs/maestro-stage-p1b-handoff.md)。

基于冻结身份基线，已有 HUMAN/WorkItem 数据库授权及离线 `scripts/authorization_admin.py`。安装、撤销、角色新版本和原 key 恢复见[开发说明](../../docs/authorization-development.md)；工具仅在显式 dev/test 环境由本机 OS 操作者运行，没有公开授予权限 API。P1b.3 当时沿用 `f4c7b2a1e903`；当前 P1b.4 已迁移到 `9c2e7a4b6d10`，MFA 不为已有成员自动赋权。真实编辑联调在独立可丢弃测试库安装虚构 WorkItem/角色/ACL，开发库保持原有业务授权事实。

## 组织 guard 的独立并发基线

先明确设置独立 PostgreSQL `TEST_DATABASE_URL`（可重置的 `stage_test*`/`stage_ci*` 数据库）。以下脚本前后清理该库的应用表，不能与同库测试并行；为获得可比较数值，也应避开其它压测/全量回归：

```bash
STAGE_ALLOW_TEST_DATABASE_RESET=1 uv run python scripts/benchmark_human_work.py \
  --requests 48 --output /tmp/stage-human-work-baseline.json
```

脚本实际运行 ASGI、Runtime、人员认证、授权与 PG；使用合成单人员、真实测试签名和模拟 JWKS，不测真实 IdP/TCP/TLS/Redis。报告带软件版本、源码摘要、并发/连接池配置、延迟/吞吐、guard/连接获取耗时及一致性断言。超时/取消用 `pg_blocking_pids` 确认真实阻塞并核验释放。基线只是当前短操作的观测，不作生产容量承诺；数据库正确性仍由独立迁移、撤权竞争与真实协议用例证明。

## 交付冻结与浏览器 CI

本机工作区开发服务与可重现交付版本分别识别。冻结工具、源码/依赖/迁移/制品摘要及真实 Chromium CI 的运行方法见[冻结说明](release-freeze.md)。完成审计整改不表示产品路线图 P2/P3 已交付；本轮证据见[整改复查](../../docs/reviews/p2p3-architecture-closeout-2026-10-26.md)。
