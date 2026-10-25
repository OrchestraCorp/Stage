---
title: P1b 首条身份切片实施与验证记录
status: validated-first-slice
implementation_status: first-slice-implemented-rest-of-p1b-pending
review_date: 2026-10-25
policy_effect: none
---

# P1b 首条身份切片实施与验证记录

用户于 2026-10-25 确认[身份表结构设计](../identity-schema-design.md)，并授权整理文档后开始实施。本轮范围为真实 API 凭据验证、内部主体映射、租户认证来源准入、tenant/membership、最小当前身份读取 API 和受控 bootstrap。完整 P1b 的角色版本/任职/资源 loader、ServiceGrant、Agent/Delegation、租户 IAM 管理命令继续后续切片；Passkey、签名准入与 WASM 按 P1c/P1d 推进。

本轮首条切片已实现并在云开发设备验证。既有 P1a 历史记录保留；I1、O1、F1 的完整目标不因局部用例通过而整体计为完成。

后续复盘的发现、修复及新的验证结果见[身份切片复查](identity-first-slice-review-2026-10-25.md)。下方数量保留首次实现时的验证批次。

## 1. 已确认的设计与落档范围

- 成员记录是一段加入经历；同 tenant/principal 最多一条未终结经历，终结后重入创建新引用。
- 角色版本由任职固定；Agent 登记、workload 绑定与委托固定其成员经历，旧授权不能随主体重新启用。
- ServiceGrant 和 Delegation 逐条保存 action-target 配对；本轮只同步目标合同，尚不建设这些授权路径。
- 租户明确接受 profile/version、环境及 client/assurance 条件；多个登录账号关联同一主体不扩大来源准入。
- PG17 是权威存储；普通读取/提交共享锁定 trust/principal，身份/信任 writer 独占锁定，再按序取得 tenant guard。保证范围以实际共同路径及真库竞争测试为准。
- 新迁移保留旧 ID 和数据。未映射旧行不能获得默认 tenant，也不能进入完整角色 loader；约束收紧须等待全部目标行映射完成。

已同步[身份规范](../identity-and-authorization.md)、[核心对象](../core-object-contracts.md)、[一期方案](../phase-1.md)、[P1b 入口](../p1b-implementation-entry.md)和[表结构设计](../identity-schema-design.md)。

## 2. 实现边界与证据登记

| 项目 | 本轮边界 | 当前证据 |
|---|---|---|
| 持久模型与迁移 | Principal 版本/epoch，trust/provider/profile/tenant binding，external identity，tenant/membership 与受控初始化 | `b7e1a92c8d40` 已应用开发 PG17，新建 10 张身份基础表、扩展 Principal；`alembic check` 无漂移，独立空 PG 库完成全历史迁移及往返验证 |
| 认证 profile | 明确 Stage audience、算法、issuer/subject namespace、凭据类型与有效期；未知或不支持 profile 拒绝 | 已实现 `stage.keycloak-access.v1`，Keycloak human/workload 互斥验证；Keycloak 26.4.7 + OpenJDK 21 真实协议/API 15 项通过 |
| 当前身份读取 | 认证→映射→租户来源准入→当前成员核验→最小投影；身份头不能覆盖可信事实 | `GET /api/identity/me`，必填 tenant_ref/profile_ref/profile_version 与 Bearer access token；OpenAPI 同步，正向与拒绝用例通过 |
| 受控 bootstrap | 部署侧显式配置；配置摘要、结果和初始事实同事务，重复执行不增权；首个登录者不成为管理员 | 离线 `scripts/identity_admin.py` 仅 dev/test，显式 DB + 本机 OS operator；SQLite/PG 均覆盖幂等、原子性、撤销和未知结果恢复 |
| 撤销与并发 | trust/principal 共享/独占锁、统一锁序与锁后重验；不依赖 Redis 失效消息 | PG17 验证当前身份读取和离线四类撤销竞争；6 个场景观察真实 blocking PID，另验证两租户共享 Principal 与全局禁用 |
| 开发服务 | 完成迁移和受保护读取冒烟，继续提供 `/health` 与 `/ready` | Stage、PostgreSQL、Redis、Keycloak 4 个 systemd 服务 active；stage-development 仅虚构 human/service 成员、业务 grants 为空；在线系统冒烟 5 项、真实协议/API 15 项通过 |

## 3. 验证与未覆盖范围

运行环境：Python 3.12、PostgreSQL 17.11、Redis 8.2.3、Keycloak 26.4.7、OpenJDK 21；锁定依赖包括 PyJWT 2.15.1、cryptography 50.0.2、httpx 0.28.1、SQLAlchemy 2.1.5、asyncpg 0.32.0、Pydantic 2.14.1。依赖来源以 `uv.lock` 为准。

已运行检查：

| 命令 | 环境与结果 | 覆盖 |
|---|---|---|
| `uv run pytest -q` | SQLite：417 passed、16 skipped | 快速回归；跳过的真实 PG/Redis/在线场景不计通过 |
| `uv run pytest -q`（配置独立测试 PG、Redis 与显式重置授权） | PG17 + Redis8：436 passed、5 skipped | 包含迁移、约束、真实行锁竞争、当前身份读取、原子初始化与未知结果恢复；5 项是须显式开启的在线系统冒烟 |
| `uv run pytest -q tests/smoke`（指向运行中的开发服务） | 5 passed | `/health`、`/ready`、并发、OpenAPI、PG/Redis 在线探测 |
| `uv run python scripts/identity_idp_smoke.py --credentials /root/.local/state/orchestra-stage/identity/credentials.json --stage-url http://127.0.0.1:8080 --tenant-ref stage-development` | 真实 Keycloak→Stage：15 checks passed | 授权码/PKCE、ID token nonce、码重放/错误或缺失 PKCE/身份类型混用/password grant 拒绝；Stage human/service、自身投影、无 Bearer/伪造头/ID token/未知租户拒绝 |
| `uv run alembic upgrade head`、`check`、`downgrade base`、`upgrade head`、`check`（独立空库 stage_test_identity_migrations） | 5 项通过 | 完整历史迁移和往返无 schema 漂移；破坏性往返仅在专用空测试库执行 |
| `uv run alembic upgrade head`、`uv run alembic check`（开发库 stage） | head=b7e1a92c8d40，无漂移 | 保留现有开发事实；bootstrap 前保存私有备份，未对开发库做 downgrade |
| `uv run pytest -q tests/identity/test_administration.py` | SQLite：20 passed、2 skipped | 显式映射/类型/namespace 拒绝、安装原子性、不授业务权、幂等冲突、四类撤销、epoch/审计、同键结果恢复、并发安装；跳过的 2 项只在 PG 验证共享/独占锁 |
| `uv run ruff check app tests scripts` 及新增 migration | 通过 | 应用/测试/脚本及新迁移静态检查 |
| `uv run ruff format --check app tests scripts` | 114 文件通过 | 仓库应用、测试和脚本格式检查 |
| `uv run mypy app tests scripts` | 111 文件通过 | 完整应用、测试和脚本类型检查 |
| `uv run python scripts/check_contract.py`、`git diff --check` | 通过 | OpenAPI/代码合同及补丁空白检查 |

真实签名 JWT 单元测试和成熟 IdP 协议冒烟分别执行。后者使用真实 Keycloak 登录表单与授权码流程、校验 state/nonce，不使用 password grant 或伪造 access token；精确客户端行为与本机 HTTPX loopback cookie 适配见[IdP 运行说明](../../deployment/identity/README.md)。它不代替 Maestro/Virtuoso 实际 Web/Desktop 客户端及 token 保管验收。

本轮 PG 锁结论只覆盖身份自身读取与离线 principal/tenant/membership/profile 撤销。原 CommandKernel 仍未接入全局 trust/principal 共享锁；角色、关系、Agent/委托写入尚无完整 writer 协议，不声明全部业务写路径已实现全局撤销。

不以本轮只读身份切片声明 I1 36 项、完整 P1b、生产身份接入或企业治理生效。角色版本、独立服务授权、Agent 委托、复杂资源关系、账户恢复、Passkey、WASM 与发布准入的完整场景继续保留原有阶段边界。

## 4. 离线开发管理与结果恢复

`scripts/identity_admin.py` 只接受 `--environment dev|test`，与 `STAGE_ENV` 一致且必须显式配置 `STAGE_DATABASE_URL`；数据库 URL 不放命令行。操作者引用来自本机 host 摘要与有效 UID，记录为 `bootstrap_operator`，不冒充普通 JWT 人类账号。该 CLI 是受控部署工具，没有对应 HTTP 管理接口。初始化计划只含显式映射和来源配置，不接收密码、角色或授权结果；本轮只建立成员并允许最小自身投影，不授业务能力。

```bash
uv run python scripts/identity_admin.py --environment dev bootstrap \
  --plan /path/to/private/bootstrap-plan.json --idempotency-key initial-identity-v1

uv run python scripts/identity_admin.py --environment dev revoke \
  --kind principal --target-ref principal-example --expected-version 1 \
  --idempotency-key revoke-principal-example-v1

uv run python scripts/identity_admin.py --environment dev result \
  --operation-type revoke.principal --idempotency-key revoke-principal-example-v1
```

bootstrap 对同环境/类型只安装一次；相同 key/输入返回已有操作，相同配置的新 key 仅登记新的重放 journal，结果指向原 bootstrap，不重新激活成员。不同配置拒绝。旧主体已存在时也拒绝隐式关联，不猜测 email 或“第一个用户”。PostgreSQL 使用事务 advisory 锁串行化这些低频离线变更；SQLite 快测采用 BEGIN IMMEDIATE，不能据此推导 PG 行锁已经通过。

撤销支持 principal、tenant、membership、profile，必须给 expected_version；成员另给 tenant_ref，profile 另给 profile_version。只允许 active/suspended 进入终态 revoked。状态、版本/epoch、最小 before/after 审计和操作结果同事务提交；没有恢复或重新授予入口。

数据库连接/提交异常返回 `result_unknown`（CLI 退出码 2）及原 operation_type/idempotency_key。按原操作者与 key 查询结果，不换 key 盲目重试。`operation_not_found` 仅表示当前查不到可见提交，保留原 key/输入重试；它不证明此前请求永远不会提交。测试通过在成功 COMMIT 后注入响应丢失，验证 bootstrap、新 key 重放和 revoke 均能恢复且不重复变更。
