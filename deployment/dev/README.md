# 当前云开发设备

运行模型为企业私有化部署：**一套 Stage、一个组织授权根、一套数据库、一个环境**；授权根是信任与治理边界，不等同于一个法人公司。客户端不选择组织，部门/项目以后按需用组织单元和 scope 管理。当前公开身份 API 为 v1，见[组织授权根语义](../../docs/organization-root-semantics.md)和[部署与 API 合同](../../docs/private-deployment-api-v1.md)。

本设备已运行 P1a 与 P1b 首条身份切片，Stage 使用单 ASGI worker，源码路径 `/root/OrchestraCorp/Stage`。Stage/PostgreSQL/Redis 以 `orchstage` 非登录系统用户运行，隔离测试 IdP 以 `orchidp` 运行，四个 systemd 服务均启用开机启动和失败重启：

| 服务 | 监听 | 用途 |
|---|---|---|
| `orchestra-stage-dev` | `127.0.0.1:8080` | Stage `/health`、`/ready`、`/api/v1/identity/me`、开发 OpenAPI |
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

`/health` 只报告进程存活。必需 PG 或 schema 不可用使 `/ready` 返回 503，连接恢复后探测自动恢复就绪；默认 Redis 可选，故障只标记 Redis unavailable。需要 Redis 的应用组合须明确设置 required 或声明相应组件依赖。基础 ready 只说明 runtime、依赖与 schema 可用，不代表部署企业身份已登记；身份入口会拒绝未知 profile/来源/成员。命令/投递组件仍只接受可信组合提供的 loader、authorizer、sink 和部署组织配置，完整业务写 API 尚未开放。

`GET /api/v1/identity/me` 要求 Bearer access token，query 仅接受 `profile_ref`、`profile_version`。服务端读取唯一部署组织，响应使用 `organization_ref`。额外 `tenant_ref`、`organization_ref` 等 query 返回 422；旧 `/api/identity/me` 和未支持的 `/api/v2/identity/me` 返回 404；`/health`、`/ready` 保留根路径。身份响应使用 `Cache-Control: no-store`。角色、Agent 委托与业务授权仍待后续 P1b 切片。

身份路径的全部响应统一设置 `Cache-Control: no-store`，包括框架生成的 422、404/405 与通用 500。`STAGE_IDENTITY_REQUEST_TIMEOUT_SECONDS` 默认 10 秒，允许大于 0 且不超过 60 秒；共同限制连接池、JWKS 和数据库锁/查询等待。到期后取消工作、完成事务清理并返回 503，不延长身份有效期。

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

本地访问可使用现有 SSH 通道转发 8080；18080 只作本机协议测试，不把数据库、Redis 或测试 IdP 暴露公网。P1b 后续任职/服务/委托、P1c WASM/签名以及生产备份恢复继续按各自验收推进。
