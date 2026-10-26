---
title: Stage 当前代码地图与完整符号索引
status: code-snapshot
date: 2026-10-26
policy_effect: none
---

# Stage 当前代码地图与完整符号索引

按当前工作区源码 AST 和实际 `create_app().openapi()` 生成索引；不执行 lifespan、不触碰业务数据库。完整性审计见[报告](reviews/stage-engineering-audit-2026-10-26.md)，全景见[架构图与目录导航](stage-architecture-panorama.md)。

索引范围：`app/` **107 个 Python 文件、16,126 行、244 个类、622 个函数/方法定义**。函数计数包含私有方法、property、协议占位和嵌套闭包，绝不等同于业务能力数量；源行数包含空行/注释。HTTP 共 25 个 method+path 操作，SQLAlchemy 37 张表。`__init__.py` 与稳定 re-export 文件也在清单内。

## 1. 分层规模

| 目录 | 文件 | 行 | 类 | 函数/方法 | 职责 |
|---|---:|---:|---:|---:|---|
| `api` | 14 | 1,901 | 21 | 72 | HTTP 协议与 DTO |
| `authz` | 11 | 1,873 | 23 | 51 | 当前业务授权 |
| `bootstrap` | 2 | 101 | 1 | 5 | 应用装配 |
| `control` | 3 | 209 | 4 | 10 | 共享本地受控管理协议 |
| `core` | 12 | 477 | 13 | 24 | 框架配置/适配辅助（不是纯 Runtime Core） |
| `delivery` | 3 | 845 | 14 | 28 | 持久投递底板 |
| `execution` | 6 | 959 | 24 | 41 | 受控事务与命令协议 |
| `features` | 4 | 162 | 6 | 4 | 具体领域操作 |
| `identity` | 9 | 1,991 | 22 | 48 | 身份来源、成员与认证证据 |
| `main` | 2 | 75 | 0 | 2 | FastAPI 入口/包 |
| `models` | 8 | 1,191 | 43 | 2 | 数据库物理事实 |
| `native_auth` | 16 | 3,727 | 25 | 125 | 本地凭据与 OAuth/OIDC |
| `providers` | 8 | 708 | 11 | 43 | 生命周期＋应用编排适配器 |
| `runtime` | 7 | 1,817 | 34 | 159 | 独立进程内组件 Core |
| `storage` | 2 | 90 | 3 | 8 | 存储类型 |

## 2. 源码目录树

完整列出 app/ Python 源文件；`__pycache__`、`.venv` 和秘密文件不进入文档。

```text
app/
├── api/  # HTTP 协议与 DTO
│   ├── native/  # 领域子包
│   │   ├── __init__.py  # 包入口 / 稳定导出
│   │   ├── account.py  # 本人会话、同意、密码与 TOTP 管理端点
│   │   ├── browser.py  # 登录、TOTP、恢复、同意与浏览器流程端点
│   │   ├── common.py  # 认证 HTTP 共享解析、安全头、来源与浏览器响应助手
│   │   ├── logout.py  # RP 退出确认与跨站 POST relay 端点
│   │   └── oauth.py  # Discovery、JWKS、code/token、UserInfo 和 revoke 协议端点
│   ├── __init__.py  # 包入口 / 稳定导出
│   ├── assignments.py  # 受保护的本人任职查询
│   ├── health.py  # 进程存活检查
│   ├── identity.py  # 当前身份投影与身份响应禁止缓存
│   ├── native_auth.py  # 原生路由组合与兼容导出
│   ├── readiness.py  # 将受管服务与数据库/Redis 健康投影为 readiness
│   ├── router.py  # 聚合 /api/v1 静态业务路由
│   └── work.py  # 已有工作项读取、标题/说明编辑、原幂等键结果查询
├── authz/  # 当前业务授权
│   ├── __init__.py  # 包入口 / 稳定导出
│   ├── administration.py  # 受控安装 action/角色版本/scope/任职/资源/ACL，版本化撤销
│   ├── assignments.py  # 从当前成员经历中筛选有效任职，受限分页并在返回前过滤到期项
│   ├── attributes.py  # ABAC 固定谓词
│   ├── authenticated.py  # 将真实人员证据和数据库授权事实接入同一锁与提交协议
│   ├── context.py  # 纯授权输入值对象与有效期/作用域规则
│   ├── decision.py  # 组合权限、关系和属性，产生可解释 allow/deny
│   ├── identity.py  # 主体、组织、成员/workload 的最小自身身份访问规则
│   ├── loader.py  # 锁定当前组织/资源事实，核验角色摘要、任职、scope 链和直接 ACL
│   ├── permissions.py  # 稳定 action 代码与内建动作策略
│   └── relations.py  # ReBAC 直接关系检查，不是通用图推理引擎
├── bootstrap/  # 应用装配
│   ├── __init__.py  # 包入口 / 稳定导出
│   └── application.py  # ApplicationRuntime 显式装配必需/可选服务，处理就绪与有序关闭
├── control/  # 共享本地受控管理协议
│   ├── __init__.py  # 包入口 / 稳定导出
│   ├── contracts.py  # 跨领域本地管理的公共输入/结果/错误合同
│   └── operations.py  # 受控管理互斥、环境检查、幂等 journal/replay 和 unknown 结果恢复
├── core/  # 框架配置/适配辅助（不是纯 Runtime Core）
│   ├── __init__.py  # 包入口 / 稳定导出
│   ├── audit.py  # 早期审计值对象
│   ├── config.py  # 部署级 Settings、PG/Redis URL 与原生发行配置校验
│   ├── db.py  # SQLAlchemy engine/session 工厂、UTC/隔离级别和存储租约依赖
│   ├── enums.py  # 主体、生命周期与决策枚举
│   ├── errors.py  # 统一脱敏 HTTP 错误映射与 request-id
│   ├── idempotency.py  # 早期幂等键值对象
│   ├── middleware.py  # 请求关联、异常边界与访问日志查询串脱敏
│   ├── outbox.py  # 早期消息值对象
│   ├── redis.py  # 有连接数和超时限制的 Redis 客户端工厂
│   ├── schemas.py  # 系统探针与错误公共响应 DTO
│   └── time.py  # UTC 时钟小工具
├── delivery/  # 持久投递底板
│   ├── __init__.py  # 包入口 / 稳定导出
│   ├── contracts.py  # 投递信封、回执、对账、sink 与当前投递授权协议
│   └── worker.py  # 短事务领取、事务外投递、持久租约/fencing、unknown 对账与关闭排空
├── execution/  # 受控事务与命令协议
│   ├── __init__.py  # 包入口 / 稳定导出
│   ├── contracts.py  # 可信 ExecutionContext、命令/效果/结果 DTO、loader/coordinator/validator 接口
│   ├── encoding.py  # 有界 JSON 规范化、复制与摘要，支撑幂等与审计
│   ├── errors.py  # 版本冲突、授权拒绝、截止、提交未知和事务所有权异常
│   ├── kernel.py  # 唯一命令协调者：当前授权、幂等/版本、领域 handler、结果/审计/outbox 原子提交与安全结果恢复
│   └── storage.py  # 受管理 UoW、事务所有权保护和 PostgreSQL 组织 guard
├── features/  # 具体领域操作
│   ├── work/  # 领域子包
│   │   ├── __init__.py  # 包入口 / 稳定导出
│   │   ├── commands.py  # 历史 ClaimWork/SubmitDelivery/AcceptDelivery DTO 草图
│   │   └── details.py  # 当前唯一默认领域写操作 work.details.update
│   └── __init__.py  # 包入口 / 稳定导出
├── identity/  # 身份来源、成员与认证证据
│   ├── __init__.py  # 包入口 / 稳定导出
│   ├── administration.py  # dev/test 身份 bootstrap 与版本化撤销
│   ├── assurance.py  # 组织登录 MFA 策略及精确 profile/version 强度规则
│   ├── assurance_administration.py  # 受控变更组织 MFA 策略，预期版本与组织 epoch 协调
│   ├── assurance_verification.py  # 校验当前 profile、ACR/AMR、auth_time 和策略版本/摘要
│   ├── authentication.py  # 中立 AccessTokenProfile、JWKS/签名验证与 VerifiedCredential
│   ├── evidence.py  # 服务器生成的短期人员认证证据与审计白名单
│   ├── native.py  # 原生 access JWT adapter，当前账号/会话事实与 Principal 映射重验
│   └── service.py  # 发行来源/trust/profile→精确账号映射→单组织成员，签发短期 evidence 并提供锁内重验
├── models/  # 数据库物理事实
│   ├── __init__.py  # 包入口 / 稳定导出
│   ├── authorization.py  # 8 张动作、角色、scope、资源、任职、关系物理表
│   ├── base.py  # SQLAlchemy Base、主键与时间列 mixin
│   ├── execution.py  # 5 张命令、审计、outbox、attempt、guard 表
│   ├── identity.py  # Principal 人员/服务主体表
│   ├── identity_foundation.py  # 身份来源/trust/profile、组织/成员、MFA、workload、bootstrap/journal 等 11 张表及 mixin
│   ├── native_auth.py  # 11 张原生凭据、客户端、登录/RP/SSO、同意、退出、code/refresh、安全事件和限流表
│   └── work.py  # 当前最小工作项事实表
├── native_auth/  # 本地凭据与 OAuth/OIDC
│   ├── __init__.py  # 包入口 / 稳定导出
│   ├── administration.py  # 将本地账号显式绑定既有 HUMAN 与成员经历
│   ├── browser.py  # 浏览器 SSO 复用与用户同意，保持原认证时间、当前策略期限和固定授权请求
│   ├── client_administration.py  # 受控多客户端配置/轮换/禁用及独立 Stage 业务来源准入
│   ├── config.py  # 规范化固定 issuer、生命周期与来源预算
│   ├── credentials.py  # 密码/TOTP/恢复和单次重绑授权，账号失败计数、凭据版本与当前会话撤销
│   ├── crypto.py  # Argon2id 有界计算、加密 TOTP、随机秘密/摘要与恢复码
│   ├── current_session.py  # 独立的当前账号、客户端和会话事实重验，调用方保有锁序
│   ├── errors.py  # 脱敏凭据错误和提交未知
│   ├── freshness.py  # 锁内按当前组织策略和操作上限计算认证截止时间
│   ├── keys.py  # 私有 RSA/Fernet 文件检查、签名/public JWKS、access 与 logout hint 验签
│   ├── logout.py  # RP 退出请求/确认、Cookie/CSRF 绑定、当前浏览器相关 RP 会话撤销
│   ├── oauth.py  # PKCE、client auth、token/refresh/revoke/UserInfo
│   ├── protocol.py  # OAuth 值对象、范围规范化、响应及错误合同，无数据库依赖
│   ├── redirects.py  # 精确回调与 issuer/RP 主机隔离
│   └── sessions.py  # 当前人员的会话/应用授权列表、单个撤销、全退出和同意撤销
├── providers/  # 生命周期＋应用编排适配器
│   ├── __init__.py  # 包入口 / 稳定导出
│   ├── assignments.py  # 本人任职应用服务：生命周期租约、真实身份、当前授权读取与截止复核
│   ├── execution.py  # 内部通用命令/投递装配工厂
│   ├── human.py  # 公共 HumanOperations.request 与完整人员 kernel 装配
│   ├── identity.py  # IdentityFacade、固定存储依赖和验证器关闭
│   ├── native_auth.py  # 固定部署绑定检查、原生各服务工厂、操作期限和 STORAGE lease
│   ├── storage.py  # 数据库 schema/head、PG/Redis 连接池及周期探测、健康状态通知
│   └── work.py  # 实际人员工作项编排入口
├── runtime/  # 独立进程内组件 Core
│   ├── README.md
│   ├── __init__.py  # 包入口 / 稳定导出
│   ├── context.py  # ComponentContext 的稳定导出入口
│   ├── contracts.py  # 组件/服务键/绑定身份、状态、就绪和关闭报告
│   ├── effects.py  # 资源 effect 登记、准备与依赖有序清理，失败可观察
│   ├── events.py  # 进程内 emit/parallel/serial/bail/waterfall
│   ├── host.py  # 可信组件注册、依赖重协调、generation、调用 pin、作用域、受监督任务与关闭屏障
│   └── leases.py  # CallScope 的导出入口
├── storage/  # 存储类型
│   ├── __init__.py  # 包入口 / 稳定导出
│   └── types.py  # 精确标识字符串与 UTC 时间存储适配，统一数据库比较与时间语义
├── __init__.py  # 包入口 / 稳定导出
└── main.py  # FastAPI 工厂、lifespan、路由与错误/安全中间件挂载
```

## 3. 逐文件职责、类与函数

以下包含公开、私有和嵌套定义。公开入口先列，内部辅助放入折叠区；括号中的源码行号可定位，不把私有方法列为稳定调用合同。参数签名与返回注解来自实际源码，方法 docstring 若存在则附首段；函数具体语义仍以源文件及审计为准。

### app/__init__.py

包初始化与公开符号导出。

[打开源码](../app/__init__.py) · 1 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/api/__init__.py

包初始化与公开符号导出。

[打开源码](../app/api/__init__.py) · 1 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/api/assignments.py

受保护的本人任职查询；解析 profile/version 和分页参数。

[打开源码](../app/api/assignments.py) · 95 行

类：[`AssignmentsQuery`](../app/api/assignments.py#L28)。

主要可调用定义：

- [`list_self_assignments`](../app/api/assignments.py#L48) — L48

  ```python
  async list_self_assignments(request: Request, query: Annotated[AssignmentsQuery, Query()], credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)]) -> AssignmentList | JSONResponse
  ```

### app/api/health.py

进程存活检查；不查询数据库。

[打开源码](../app/api/health.py) · 22 行

主要可调用定义：

- [`health`](../app/api/health.py#L18) — L18

  ```python
  async health(request: Request) -> HealthResponse
  ```
  Report that the Stage process is alive without performing dependency checks.

### app/api/identity.py

当前身份投影与身份响应禁止缓存。

[打开源码](../app/api/identity.py) · 121 行

类：[`IdentityQuery`](../app/api/identity.py#L30)；[`IdentityCacheControlMiddleware`](../app/api/identity.py#L37)。

主要可调用定义：

- [`read_self_identity`](../app/api/identity.py#L73) — L73

  ```python
  async read_self_identity(request: Request, query: Annotated[IdentityQuery, Query()], credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)]) -> SelfIdentity | JSONResponse
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`IdentityCacheControlMiddleware.__init__`](../app/api/identity.py#L40) — L40

  ```python
  IdentityCacheControlMiddleware.__init__(self, app: ASGIApp) -> None
  ```
- [`IdentityCacheControlMiddleware.__call__`](../app/api/identity.py#L43) — L43

  ```python
  async IdentityCacheControlMiddleware.__call__(self, scope: Scope, receive: Receive, send: Send) -> None
  ```
- [`IdentityCacheControlMiddleware.__call__.private_send`](../app/api/identity.py#L55) — L55

  ```python
  async IdentityCacheControlMiddleware.__call__.private_send(message: Message) -> None
  ```

</details>

### app/api/native/__init__.py

包初始化与公开符号导出。

[打开源码](../app/api/native/__init__.py) · 1 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/api/native/account.py

本人会话、同意、密码与 TOTP 管理端点。

[打开源码](../app/api/native/account.py) · 275 行

类：[`PasswordChange`](../app/api/native/account.py#L34)；[`TotpReset`](../app/api/native/account.py#L40)；[`FactorResetResponse`](../app/api/native/account.py#L46)；[`AccountSession`](../app/api/native/account.py#L60)；[`AccountSessionPage`](../app/api/native/account.py#L72)；[`AccountConsent`](../app/api/native/account.py#L78)；[`AccountConsentPage`](../app/api/native/account.py#L88)；[`CredentialVersionResponse`](../app/api/native/account.py#L94)。

主要可调用定义：

- [`account_sessions`](../app/api/native/account.py#L139) — L139

  ```python
  async account_sessions(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)]) -> dict[str, Any]
  ```
- [`account_revoke_session`](../app/api/native/account.py#L151) — L151

  ```python
  async account_revoke_session(request: Request, session_ref: str, credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)]) -> Response
  ```
- [`account_revoke_all_sessions`](../app/api/native/account.py#L168) — L168

  ```python
  async account_revoke_all_sessions(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)]) -> dict[str, Any]
  ```
- [`account_consents`](../app/api/native/account.py#L184) — L184

  ```python
  async account_consents(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)]) -> dict[str, Any]
  ```
- [`account_revoke_consent`](../app/api/native/account.py#L196) — L196

  ```python
  async account_revoke_consent(request: Request, client_id: str, credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)]) -> Response
  ```
- [`change_password`](../app/api/native/account.py#L240) — L240

  ```python
  async change_password(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)]) -> Response
  ```
- [`reset_totp`](../app/api/native/account.py#L263) — L263

  ```python
  async reset_totp(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)]) -> FactorResetResponse
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_account_page`](../app/api/native/account.py#L117) — L117

  ```python
  _account_page(request: Request) -> dict[str, Any]
  ```
- [`_empty_account_operation`](../app/api/native/account.py#L128) — L128

  ```python
  async _empty_account_operation(request: Request) -> None
  ```
- [`_json_pairs`](../app/api/native/account.py#L208) — L208

  ```python
  _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]
  ```
- [`_account_body`](../app/api/native/account.py#L217) — L217

  ```python
  async _account_body(request: Request) -> Any
  ```
- [`_json_contract`](../app/api/native/account.py#L229) — L229

  ```python
  _json_contract(model: type[BaseModel]) -> dict[str, Any]
  ```

</details>

### app/api/native/browser.py

登录、TOTP、恢复、同意与浏览器流程端点。

[打开源码](../app/api/native/browser.py) · 299 行

主要可调用定义：

- [`authorize`](../app/api/native/browser.py#L202) — L202

  ```python
  async authorize(request: Request) -> Response
  ```
- [`login`](../app/api/native/browser.py#L251) — L251

  ```python
  async login(request: Request) -> Response
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_login_page`](../app/api/native/browser.py#L36) — L36

  ```python
  _login_page(issuer: str, request_ref: str, csrf_token: str, progress: LoginProgress, *, invalid: bool=False) -> HTMLResponse
  ```
- [`_login_page.form`](../app/api/native/browser.py#L48) — L48

  ```python
  _login_page.form(stage: str, content: str, submit: str) -> str
  ```
- [`_consent_page`](../app/api/native/browser.py#L123) — L123

  ```python
  _consent_page(issuer: str, request_ref: str, csrf_token: str, outcome: Any) -> HTMLResponse
  ```
- [`_advance_login`](../app/api/native/browser.py#L152) — L152

  ```python
  async _advance_login(facade: NativeAuthFacade, services: NativeAuthServices, context: tuple[str, str, str], *, consent: bool | None=None) -> tuple[Response, bool]
  ```

</details>

### app/api/native/common.py

认证 HTTP 共享解析、安全头、来源与浏览器响应助手。

[打开源码](../app/api/native/common.py) · 339 行

类：[`NativeHTTPError`](../app/api/native/common.py#L34)；[`NativeRoute`](../app/api/native/common.py#L70)；[`OAuthFailure`](../app/api/native/common.py#L102)；[`NativeAuthSecurityMiddleware`](../app/api/native/common.py#L107)。

主要可调用定义：

- [`NativeRoute.get_route_handler`](../app/api/native/common.py#L71) — L71

  ```python
  NativeRoute.get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]
  ```
- [`native_services`](../app/api/native/common.py#L152) — L152

  ```python
  async native_services(request: Request) -> AsyncIterator[tuple[NativeAuthFacade, NativeAuthServices]]
  ```
- [`parse_pairs`](../app/api/native/common.py#L162) — L162

  ```python
  parse_pairs(raw: bytes) -> dict[str, str]
  ```
- [`read_body`](../app/api/native/common.py#L180) — L180

  ```python
  async read_body(request: Request) -> bytes
  ```
- [`read_form`](../app/api/native/common.py#L189) — L189

  ```python
  async read_form(request: Request) -> dict[str, str]
  ```
- [`require_fields`](../app/api/native/common.py#L197) — L197

  ```python
  require_fields(values: dict[str, str], required: set[str], optional: set[str] | None=None) -> None
  ```
- [`require_same_origin`](../app/api/native/common.py#L204) — L204

  ```python
  require_same_origin(request: Request, issuer: str) -> None
  ```
- [`login_cookie_name`](../app/api/native/common.py#L213) — L213

  ```python
  login_cookie_name(issuer: str) -> str
  ```
- [`sso_cookie_name`](../app/api/native/common.py#L217) — L217

  ```python
  sso_cookie_name(issuer: str) -> str
  ```
- [`logout_cookie_name`](../app/api/native/common.py#L221) — L221

  ```python
  logout_cookie_name(issuer: str) -> str
  ```
- [`cookie_secret`](../app/api/native/common.py#L225) — L225

  ```python
  cookie_secret(request: Request, name: str, *, required: bool) -> str | None
  ```
- [`get_browser_secret`](../app/api/native/common.py#L240) — L240

  ```python
  get_browser_secret(request: Request, issuer: str) -> str
  ```
- [`set_browser_cookie`](../app/api/native/common.py#L246) — L246

  ```python
  set_browser_cookie(response: Response, name: str, secret: str, issuer: str, max_age: int) -> None
  ```
- [`clear_browser_cookie`](../app/api/native/common.py#L252) — L252

  ```python
  clear_browser_cookie(response: Response, name: str, issuer: str) -> None
  ```
- [`hidden_input`](../app/api/native/common.py#L256) — L256

  ```python
  hidden_input(name: str, value: str) -> str
  ```
- [`callback_origin`](../app/api/native/common.py#L260) — L260

  ```python
  callback_origin(redirect_uri: str | None) -> str
  ```
  Only consume the server-validated registration; never insert path/query in CSP.
- [`authorization_response`](../app/api/native/common.py#L279) — L279

  ```python
  authorization_response(result: AuthorizationResponse) -> Response
  ```
  Render only the callback already validated by the protocol service.
- [`form_contract`](../app/api/native/common.py#L306) — L306

  ```python
  form_contract(required: list[str], optional: list[str] | None=None) -> dict[str, Any]
  ```
- [`bearer_token`](../app/api/native/common.py#L324) — L324

  ```python
  bearer_token(credentials: HTTPAuthorizationCredentials | None, request: Request) -> str
  ```
- [`make_oauth_router`](../app/api/native/common.py#L330) — L330

  ```python
  make_oauth_router() -> APIRouter
  ```
  Keep protocol routes on the same error and native-service availability boundary.

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`NativeHTTPError.__init__`](../app/api/native/common.py#L35) — L35

  ```python
  NativeHTTPError.__init__(self, error: str='invalid_request', status: int=400) -> None
  ```
- [`_oauth_error`](../app/api/native/common.py#L40) — L40

  ```python
  _oauth_error(error: str, status: int) -> JSONResponse
  ```
- [`_native_error`](../app/api/native/common.py#L51) — L51

  ```python
  _native_error(request: Request, error: str, status: int) -> JSONResponse
  ```
- [`NativeRoute.get_route_handler.protected`](../app/api/native/common.py#L74) — L74

  ```python
  async NativeRoute.get_route_handler.protected(request: Request) -> Response
  ```
- [`NativeAuthSecurityMiddleware.__init__`](../app/api/native/common.py#L110) — L110

  ```python
  NativeAuthSecurityMiddleware.__init__(self, app: ASGIApp) -> None
  ```
- [`NativeAuthSecurityMiddleware.__call__`](../app/api/native/common.py#L113) — L113

  ```python
  async NativeAuthSecurityMiddleware.__call__(self, scope: Scope, receive: Receive, send: Send) -> None
  ```
- [`NativeAuthSecurityMiddleware.__call__.secure_send`](../app/api/native/common.py#L126) — L126

  ```python
  async NativeAuthSecurityMiddleware.__call__.secure_send(message: Message) -> None
  ```

</details>

### app/api/native/logout.py

RP 退出确认与跨站 POST relay 端点。

[打开源码](../app/api/native/logout.py) · 195 行

主要可调用定义：

- [`logout_confirmation`](../app/api/native/logout.py#L45) — L45

  ```python
  async logout_confirmation(request: Request) -> Response
  ```
- [`logout`](../app/api/native/logout.py#L157) — L157

  ```python
  async logout(request: Request) -> Response
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_begin_logout`](../app/api/native/logout.py#L51) — L51

  ```python
  async _begin_logout(request: Request, values: dict[str, str]) -> Response
  ```
- [`_logout_relay`](../app/api/native/logout.py#L94) — L94

  ```python
  _logout_relay(issuer: str, values: dict[str, str]) -> HTMLResponse
  ```

</details>

### app/api/native/oauth.py

Discovery、JWKS、code/token、UserInfo 和 revoke 协议端点。

[打开源码](../app/api/native/oauth.py) · 219 行

类：[`TokenResponse`](../app/api/native/oauth.py#L31)；[`UserInfo`](../app/api/native/oauth.py#L41)。

主要可调用定义：

- [`client_authentication`](../app/api/native/oauth.py#L47) — L47

  ```python
  client_authentication(request: Request, values: dict[str, str]) -> dict[str, Any]
  ```
  RFC 6749 Basic credentials have form-encoded components before base64.
- [`discovery`](../app/api/native/oauth.py#L80) — L80

  ```python
  async discovery(request: Request) -> dict[str, Any]
  ```
- [`oauth_metadata`](../app/api/native/oauth.py#L124) — L124

  ```python
  async oauth_metadata(request: Request) -> dict[str, Any]
  ```
- [`jwks`](../app/api/native/oauth.py#L145) — L145

  ```python
  async jwks(request: Request) -> dict[str, Any]
  ```
- [`token`](../app/api/native/oauth.py#L163) — L163

  ```python
  async token(request: Request) -> dict[str, Any]
  ```
- [`revoke`](../app/api/native/oauth.py#L194) — L194

  ```python
  async revoke(request: Request) -> Response
  ```
- [`userinfo`](../app/api/native/oauth.py#L212) — L212

  ```python
  async userinfo(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)]) -> dict[str, Any]
  ```

### app/api/native_auth.py

原生路由组合与兼容导出；实际职责位于 native 子包。

[打开源码](../app/api/native_auth.py) · 30 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/api/readiness.py

将受管服务与数据库/Redis 健康投影为 readiness。

[打开源码](../app/api/readiness.py) · 35 行

类：[`ReadyResponse`](../app/api/readiness.py#L13)。

主要可调用定义：

- [`readiness`](../app/api/readiness.py#L25) — L25

  ```python
  async readiness(request: Request, response: Response) -> ReadyResponse
  ```
  Check supervised dependency readiness without exposing credentials or internal diagnostics.

### app/api/router.py

聚合 /api/v1 静态业务路由。

[打开源码](../app/api/router.py) · 12 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/api/work.py

已有工作项读取、标题/说明编辑、原幂等键结果查询。

[打开源码](../app/api/work.py) · 257 行

类：[`WorkQuery`](../app/api/work.py#L46)；[`UpdateWorkDetailsRequest`](../app/api/work.py#L50)；[`WorkOperationResult`](../app/api/work.py#L54)。

主要可调用定义：

- [`WorkOperationResult.from_command`](../app/api/work.py#L64) — L64

  ```python
  WorkOperationResult.from_command(cls, result: CommandResult) -> 'WorkOperationResult'
  ```
- [`read_work_item`](../app/api/work.py#L164) — L164

  ```python
  async read_work_item(request: Request, work_item_id: WorkItemID, query: Annotated[WorkQuery, Query()], credentials: Credentials) -> WorkItemView | JSONResponse
  ```
- [`update_work_details`](../app/api/work.py#L194) — L194

  ```python
  async update_work_details(request: Request, work_item_id: WorkItemID, query: Annotated[WorkQuery, Query()], credentials: Credentials, payload: UpdateWorkDetailsRequest, idempotency_key: Annotated[str, Header(alias='Idempotency-Key', pattern=IDEMPOTENCY_PATTERN, max_length=128)]) -> WorkOperationResult | JSONResponse
  ```
- [`read_work_operation`](../app/api/work.py#L235) — L235

  ```python
  async read_work_operation(request: Request, work_item_id: WorkItemID, idempotency_key: Annotated[str, Path(pattern=IDEMPOTENCY_PATTERN, max_length=128)], query: Annotated[WorkQuery, Query()], credentials: Credentials) -> WorkOperationResult | JSONResponse
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_recovery_location`](../app/api/work.py#L91) — L91

  ```python
  _recovery_location(request: Request, query: WorkQuery, work_item_id: int, idempotency_key: str) -> str
  ```
- [`_call_work`](../app/api/work.py#L97) — L97

  ```python
  async _call_work(request: Request, credentials: HTTPAuthorizationCredentials | None, operation: Callable[[HumanWorkService, str], Awaitable[Model]], *, recovery_location: str | None=None) -> Model | JSONResponse
  ```
- [`read_work_item.read`](../app/api/work.py#L170) — L170

  ```python
  async read_work_item.read(service: HumanWorkService, token: str) -> WorkItemView
  ```
- [`update_work_details.update`](../app/api/work.py#L202) — L202

  ```python
  async update_work_details.update(service: HumanWorkService, token: str) -> WorkOperationResult
  ```
- [`read_work_operation.recover`](../app/api/work.py#L242) — L242

  ```python
  async read_work_operation.recover(service: HumanWorkService, token: str) -> WorkOperationResult
  ```

</details>

### app/authz/__init__.py

包初始化与公开符号导出。

[打开源码](../app/authz/__init__.py) · 20 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/authz/administration.py

受控安装 action/角色版本/scope/任职/资源/ACL，版本化撤销。

[打开源码](../app/authz/administration.py) · 547 行

类：[`ScopePlan`](../app/authz/administration.py#L61)；[`MemberGrantPlan`](../app/authz/administration.py#L67)；[`AssignmentPlan`](../app/authz/administration.py#L74)；[`ResourcePlan`](../app/authz/administration.py#L79)；[`ACLPlan`](../app/authz/administration.py#L90)；[`InstallAuthorizationPlan`](../app/authz/administration.py#L101)；[`AuthorizationRevocationRequest`](../app/authz/administration.py#L133)。

主要可调用定义：

- [`ResourcePlan.validate_reference`](../app/authz/administration.py#L85) — L85

  ```python
  ResourcePlan.validate_reference(self) -> Self
  ```
- [`ACLPlan.validate_reference`](../app/authz/administration.py#L96) — L96

  ```python
  ACLPlan.validate_reference(self) -> Self
  ```
- [`InstallAuthorizationPlan.validate_references`](../app/authz/administration.py#L117) — L117

  ```python
  InstallAuthorizationPlan.validate_references(self) -> Self
  ```
- [`AuthorizationRevocationRequest.validate_role_revision`](../app/authz/administration.py#L142) — L142

  ```python
  AuthorizationRevocationRequest.validate_role_revision(self) -> Self
  ```
- [`work_id`](../app/authz/administration.py#L148) — L148

  ```python
  work_id(reference: str) -> int
  ```
- [`install_authorization`](../app/authz/administration.py#L339) — L339

  ```python
  async install_authorization(sessions: async_sessionmaker[AsyncSession], plan: InstallAuthorizationPlan, *, actor_ref: str, idempotency_key: str) -> AdministrationResult
  ```
  Append one bounded development role release and its explicit grants.
- [`revoke_authorization`](../app/authz/administration.py#L489) — L489

  ```python
  async revoke_authorization(sessions: async_sessionmaker[AsyncSession], request: AuthorizationRevocationRequest, *, actor_ref: str, idempotency_key: str) -> AdministrationResult
  ```
  Terminal revocation; the shared organization guard serializes commits.

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_initialized`](../app/authz/administration.py#L158) — L158

  ```python
  async _initialized(session: AsyncSession, environment: Environment) -> None
  ```
- [`_principal_locks`](../app/authz/administration.py#L170) — L170

  ```python
  async _principal_locks(session: AsyncSession, references: set[str]) -> dict[str, PrincipalTable]
  ```
- [`_root_guard`](../app/authz/administration.py#L185) — L185

  ```python
  async _root_guard(session: AsyncSession, organization_ref: str, *, expected_version: int | None=None) -> TenantGuardTable
  ```
- [`_members`](../app/authz/administration.py#L203) — L203

  ```python
  async _members(session: AsyncSession, plan: InstallAuthorizationPlan, principals: dict[str, PrincipalTable], now: datetime) -> None
  ```
- [`_active_scope`](../app/authz/administration.py#L230) — L230

  ```python
  async _active_scope(session: AsyncSession, organization_ref: str, scope_ref: str) -> None
  ```
- [`_install_scopes`](../app/authz/administration.py#L248) — L248

  ```python
  async _install_scopes(session: AsyncSession, plan: InstallAuthorizationPlan) -> None
  ```
- [`_install_role`](../app/authz/administration.py#L273) — L273

  ```python
  async _install_role(session: AsyncSession, plan: InstallAuthorizationPlan) -> None
  ```
- [`_work_resource`](../app/authz/administration.py#L326) — L326

  ```python
  async _work_resource(session: AsyncSession, organization_ref: str, resource_ref: str) -> AuthorizationResourceTable
  ```
- [`_revoke_target`](../app/authz/administration.py#L453) — L453

  ```python
  async _revoke_target(session: AsyncSession, request: AuthorizationRevocationRequest) -> RoleAssignmentTable | RelationTupleTable | RoleVersionTable | AuthorizationScopeTable | AuthorizationResourceTable
  ```

</details>

### app/authz/assignments.py

从当前成员经历中筛选有效任职，受限分页并在返回前过滤到期项。

[打开源码](../app/authz/assignments.py) · 114 行

类：[`AssignmentView`](../app/authz/assignments.py#L14)；[`AssignmentList`](../app/authz/assignments.py#L30)。

主要可调用定义：

- [`AssignmentList.current_at`](../app/authz/assignments.py#L38) — L38

  ```python
  AssignmentList.current_at(self, now: datetime) -> 'AssignmentList'
  ```
- [`load_self_assignments`](../app/authz/assignments.py#L50) — L50

  ```python
  async load_self_assignments(session: AsyncSession, evidence: HumanAuthenticationEvidence, *, now: datetime, limit: int=20, after: str | None=None) -> AssignmentList
  ```
  The caller holds identity locks and the organization guard after validation.

### app/authz/attributes.py

ABAC 固定谓词；按时间、分类、状态、版本等返回拒绝原因。

[打开源码](../app/authz/attributes.py) · 56 行

主要可调用定义：

- [`evaluate_attributes`](../app/authz/attributes.py#L11) — L11

  ```python
  evaluate_attributes(attributes: AttributeContext, *, require_object_version: bool=False, allowed_resource_states: tuple[str, ...]=()) -> tuple[str, ...]
  ```

### app/authz/authenticated.py

将真实人员证据和数据库授权事实接入同一锁与提交协议。

[打开源码](../app/authz/authenticated.py) · 101 行

类：[`HumanIdentityVerifier`](../app/authz/authenticated.py#L16)；[`AuthenticatedAuthorizationLoader`](../app/authz/authenticated.py#L27)。

主要可调用定义：

- [`HumanIdentityVerifier.acquire_human_locks`](../app/authz/authenticated.py#L17) — L17

  ```python
  async HumanIdentityVerifier.acquire_human_locks(self, session: AsyncSession, evidence: HumanAuthenticationEvidence) -> None
  ```
- [`HumanIdentityVerifier.validate_human`](../app/authz/authenticated.py#L19) — L19

  ```python
  async HumanIdentityVerifier.validate_human(self, session: AsyncSession, evidence: HumanAuthenticationEvidence, now: datetime) -> None
  ```
- [`AuthenticatedAuthorizationLoader.acquire_locks`](../app/authz/authenticated.py#L48) — L48

  ```python
  async AuthenticatedAuthorizationLoader.acquire_locks(self, session: AsyncSession, context: ExecutionContext) -> None
  ```
- [`AuthenticatedAuthorizationLoader.load`](../app/authz/authenticated.py#L65) — L65

  ```python
  async AuthenticatedAuthorizationLoader.load(self, session: AsyncSession, context: ExecutionContext, *, action: str, target_ref: str, expected_version: int | None, now: datetime) -> AuthorizationContext
  ```
- [`AuthenticatedAuthorizationLoader.validate_before_commit`](../app/authz/authenticated.py#L92) — L92

  ```python
  async AuthenticatedAuthorizationLoader.validate_before_commit(self, session: AsyncSession, context: ExecutionContext, now: datetime) -> None
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`AuthenticatedAuthorizationLoader.__init__`](../app/authz/authenticated.py#L28) — L28

  ```python
  AuthenticatedAuthorizationLoader.__init__(self, identity: HumanIdentityVerifier) -> None
  ```
- [`AuthenticatedAuthorizationLoader._evidence`](../app/authz/authenticated.py#L34) — L34

  ```python
  AuthenticatedAuthorizationLoader._evidence(context: ExecutionContext) -> HumanAuthenticationEvidence
  ```
- [`AuthenticatedAuthorizationLoader._check_locks`](../app/authz/authenticated.py#L56) — L56

  ```python
  AuthenticatedAuthorizationLoader._check_locks(self, session: AsyncSession, context: ExecutionContext) -> HumanAuthenticationEvidence
  ```

</details>

### app/authz/context.py

纯授权输入值对象与有效期/作用域规则；部分 Agent/Delegation 为历史兼容类型，不是开放功能。

[打开源码](../app/authz/context.py) · 178 行

类：[`RoleAssignment`](../app/authz/context.py#L56)；[`Delegation`](../app/authz/context.py#L94)；[`RelationTuple`](../app/authz/context.py#L128)；[`AttributeContext`](../app/authz/context.py#L141)；[`AuthorizationContext`](../app/authz/context.py#L153)。

主要可调用定义：

- [`is_aware`](../app/authz/context.py#L18) — L18

  ```python
  is_aware(value: datetime | None) -> bool
  ```
- [`utc_or_none`](../app/authz/context.py#L22) — L22

  ```python
  utc_or_none(value: datetime | None) -> datetime | None
  ```
- [`validity_failure`](../app/authz/context.py#L30) — L30

  ```python
  validity_failure(now: datetime, *, valid_from: datetime | None, expires_at: datetime | None, status: str, revoked_at: datetime | None=None) -> str | None
  ```
  Describe a validity failure without conflating revocation with expiry.
- [`RoleAssignment.inactive_reason_at`](../app/authz/context.py#L73) — L73

  ```python
  RoleAssignment.inactive_reason_at(self, now: datetime) -> str | None
  ```
- [`RoleAssignment.is_active_at`](../app/authz/context.py#L76) — L76

  ```python
  RoleAssignment.is_active_at(self, now: datetime) -> bool
  ```
- [`RoleAssignment.covers`](../app/authz/context.py#L79) — L79

  ```python
  RoleAssignment.covers(self, *, organization_ref: str, resource_scopes: tuple[str, ...]) -> bool
  ```
  Match an explicitly granted scope against authoritative ancestors.
- [`Delegation.inactive_reason_at`](../app/authz/context.py#L107) — L107

  ```python
  Delegation.inactive_reason_at(self, now: datetime) -> str | None
  ```
- [`Delegation.is_active_at`](../app/authz/context.py#L116) — L116

  ```python
  Delegation.is_active_at(self, now: datetime) -> bool
  ```
- [`Delegation.permits`](../app/authz/context.py#L119) — L119

  ```python
  Delegation.permits(self, *, action: str, resource_ref: str, organization_ref: str) -> bool
  ```
- [`RelationTuple.is_active_at`](../app/authz/context.py#L136) — L136

  ```python
  RelationTuple.is_active_at(self, now: datetime) -> bool
  ```
- [`AuthorizationContext.agent_ref`](../app/authz/context.py#L173) — L173

  ```python
  AuthorizationContext.agent_ref(self) -> str | None
  ```
- [`AuthorizationContext.delegation_ref`](../app/authz/context.py#L177) — L177

  ```python
  AuthorizationContext.delegation_ref(self) -> str | None
  ```

### app/authz/decision.py

组合权限、关系和属性，产生可解释 allow/deny。

[打开源码](../app/authz/decision.py) · 247 行

类：[`AuthorizationDecision`](../app/authz/decision.py#L13)；[`AuthorizationEngine`](../app/authz/decision.py#L31)。

主要可调用定义：

- [`AuthorizationEngine.decide`](../app/authz/decision.py#L38) — L38

  ```python
  AuthorizationEngine.decide(self, context: AuthorizationContext) -> AuthorizationDecision
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`AuthorizationEngine._deny`](../app/authz/decision.py#L226) — L226

  ```python
  AuthorizationEngine._deny(context: AuthorizationContext, now: datetime, *reasons: str, outcome: DecisionOutcome=DecisionOutcome.DENIED, matched_roles: tuple[str, ...]=(), matched_relations: tuple[str, ...]=()) -> AuthorizationDecision
  ```

</details>

### app/authz/identity.py

主体、组织、成员/workload 的最小自身身份访问规则。

[打开源码](../app/authz/identity.py) · 18 行

主要可调用定义：

- [`permits_self_identity`](../app/authz/identity.py#L4) — L4

  ```python
  permits_self_identity(*, principal_type: str, principal_active: bool, organization_active: bool, membership_active: bool, workload_active: bool) -> bool
  ```

### app/authz/loader.py

锁定当前组织/资源事实，核验角色摘要、任职、scope 链和直接 ACL。

[打开源码](../app/authz/loader.py) · 446 行

类：[`ValidatedRoleVersion`](../app/authz/loader.py#L73)；[`DatabaseAuthorizationLoader`](../app/authz/loader.py#L179)。

主要可调用定义：

- [`role_permissions_digest`](../app/authz/loader.py#L40) — L40

  ```python
  role_permissions_digest(organization_ref: str, role_ref: str, role_revision: int, policy_release_ref: str, actions: Iterable[str]) -> str
  ```
  Canonical digest of the immutable role-version capability set.
- [`load_role_version`](../app/authz/loader.py#L80) — L80

  ```python
  async load_role_version(session: AsyncSession, organization_ref: str, assignment: RoleAssignmentTable) -> ValidatedRoleVersion | None
  ```
  Read a pinned capability set under the caller-held organization guard.
- [`load_scope_chain`](../app/authz/loader.py#L153) — L153

  ```python
  async load_scope_chain(session: AsyncSession, organization_ref: str, scope_ref: str) -> tuple[tuple[str, int], ...]
  ```
  Require every ancestor to be active; reject cycles, absence and excess depth.
- [`DatabaseAuthorizationLoader.acquire_locks`](../app/authz/loader.py#L194) — L194

  ```python
  async DatabaseAuthorizationLoader.acquire_locks(self, session: AsyncSession, context: ExecutionContext) -> None
  ```
  Acquire global identity locks before the organization's write guard.
- [`DatabaseAuthorizationLoader.load`](../app/authz/loader.py#L216) — L216

  ```python
  async DatabaseAuthorizationLoader.load(self, session: AsyncSession, context: ExecutionContext, *, action: str, target_ref: str, expected_version: int | None, now: datetime) -> AuthorizationContext
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`DatabaseAuthorizationLoader.__init__`](../app/authz/loader.py#L191) — L191

  ```python
  DatabaseAuthorizationLoader.__init__(self) -> None
  ```

</details>

### app/authz/permissions.py

稳定 action 代码与内建动作策略；业务不用角色名分支。

[打开源码](../app/authz/permissions.py) · 114 行

类：[`Permission`](../app/authz/permissions.py#L13)；[`RoleCode`](../app/authz/permissions.py#L24)；[`ActionPolicy`](../app/authz/permissions.py#L50)。

主要可调用定义：

- [`permissions_of`](../app/authz/permissions.py#L110) — L110

  ```python
  permissions_of(roles: Collection[RoleCode]) -> frozenset[Permission]
  ```

### app/authz/relations.py

ReBAC 直接关系检查，不是通用图推理引擎。

[打开源码](../app/authz/relations.py) · 32 行

主要可调用定义：

- [`has_relation`](../app/authz/relations.py#L7) — L7

  ```python
  has_relation(relations: Iterable[RelationTuple], *, subject_ref: str, relation: str, resource_ref: str, now: datetime) -> bool
  ```
- [`relation_for_group`](../app/authz/relations.py#L19) — L19

  ```python
  relation_for_group(relations: Iterable[RelationTuple], *, subjects: tuple[str, ...], relation_names: frozenset[str], resource_ref: str, now: datetime) -> str | None
  ```

### app/bootstrap/__init__.py

包初始化与公开符号导出。

[打开源码](../app/bootstrap/__init__.py) · 1 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/bootstrap/application.py

ApplicationRuntime 显式装配必需/可选服务，处理就绪与有序关闭。

[打开源码](../app/bootstrap/application.py) · 100 行

类：[`ApplicationRuntime`](../app/bootstrap/application.py#L17)。

主要可调用定义：

- [`ApplicationRuntime.start`](../app/bootstrap/application.py#L35) — L35

  ```python
  async ApplicationRuntime.start(self) -> None
  ```
- [`ApplicationRuntime.ready`](../app/bootstrap/application.py#L67) — L67

  ```python
  ApplicationRuntime.ready(self) -> bool
  ```
- [`ApplicationRuntime.public_status`](../app/bootstrap/application.py#L76) — L76

  ```python
  ApplicationRuntime.public_status(self) -> dict[str, str]
  ```
- [`ApplicationRuntime.close`](../app/bootstrap/application.py#L92) — L92

  ```python
  async ApplicationRuntime.close(self, timeout: float | None=None) -> CloseReport
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`ApplicationRuntime.__init__`](../app/bootstrap/application.py#L18) — L18

  ```python
  ApplicationRuntime.__init__(self, config: Settings, plugins: tuple[PluginSpec, ...]=(), *, optional_plugins: tuple[PluginSpec, ...]=()) -> None
  ```

</details>

### app/control/__init__.py

包初始化与公开符号导出。

[打开源码](../app/control/__init__.py) · 1 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/control/contracts.py

跨领域本地管理的公共输入/结果/错误合同。

[打开源码](../app/control/contracts.py) · 35 行

类：[`AdministrationError`](../app/control/contracts.py#L12)；[`OperationUnknown`](../app/control/contracts.py#L20)；[`StrictPlan`](../app/control/contracts.py#L29)；[`AdministrationResult`](../app/control/contracts.py#L33)。

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`AdministrationError.__init__`](../app/control/contracts.py#L15) — L15

  ```python
  AdministrationError.__init__(self, code: str) -> None
  ```
- [`OperationUnknown.__init__`](../app/control/contracts.py#L23) — L23

  ```python
  OperationUnknown.__init__(self, operation_type: str, idempotency_key: str) -> None
  ```

</details>

### app/control/operations.py

受控管理互斥、环境检查、幂等 journal/replay 和 unknown 结果恢复。

[打开源码](../app/control/operations.py) · 173 行

主要可调用定义：

- [`public_result`](../app/control/operations.py#L30) — L30

  ```python
  public_result(operation_ref: str, value: dict[str, Any]) -> AdministrationResult
  ```
- [`validate_operator`](../app/control/operations.py#L41) — L41

  ```python
  validate_operator(actor_ref: str, idempotency_key: str) -> None
  ```
- [`acquire_admin_lock`](../app/control/operations.py#L46) — L46

  ```python
  async acquire_admin_lock(session: AsyncSession) -> None
  ```
- [`check_database_environment`](../app/control/operations.py#L56) — L56

  ```python
  async check_database_environment(session: AsyncSession, environment: Environment) -> None
  ```
- [`operation_transaction`](../app/control/operations.py#L75) — L75

  ```python
  async operation_transaction(sessions: async_sessionmaker[AsyncSession], operation_type: str, idempotency_key: str) -> AsyncIterator[AsyncSession]
  ```
- [`find_replay`](../app/control/operations.py#L91) — L91

  ```python
  async find_replay(session: AsyncSession, actor_ref: str, operation_type: str, idempotency_key: str, digest: str) -> AdministrationResult | None
  ```
- [`append_journal`](../app/control/operations.py#L113) — L113

  ```python
  append_journal(session: AsyncSession, *, actor_ref: str, operation_type: str, idempotency_key: str, digest: str, environment: str, target_ref: str, result: dict[str, Any], before: dict[str, Any] | None=None, after: dict[str, Any] | None=None, operation_ref: str | None=None) -> AdministrationResult
  ```
- [`operation_result`](../app/control/operations.py#L147) — L147

  ```python
  async operation_result(sessions: async_sessionmaker[AsyncSession], *, environment: Environment, actor_ref: str, operation_type: str, idempotency_key: str) -> AdministrationResult
  ```
  Recover only the original operator's result in its recorded environment.

### app/core/__init__.py

包初始化与公开符号导出。

[打开源码](../app/core/__init__.py) · 1 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/core/audit.py

早期审计值对象；真实持久命令审计由 execution/models 处理。

[打开源码](../app/core/audit.py) · 21 行

类：[`AuditEntry`](../app/core/audit.py#L11)。

### app/core/config.py

部署级 Settings、PG/Redis URL 与原生发行配置校验。

[打开源码](../app/core/config.py) · 139 行

类：[`Settings`](../app/core/config.py#L56)。

主要可调用定义：

- [`validate_database_url`](../app/core/config.py#L13) — L13

  ```python
  validate_database_url(value: str) -> str
  ```
  Allow only async drivers supported by the Stage base layer.
- [`validate_redis_url`](../app/core/config.py#L33) — L33

  ```python
  validate_redis_url(value: str) -> str
  ```
  Accept explicit TCP Redis URLs without query overrides of client limits.

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`Settings._validate_database_url`](../app/core/config.py#L91) — L91

  ```python
  Settings._validate_database_url(cls, value: str) -> str
  ```
- [`Settings._validate_redis_url`](../app/core/config.py#L96) — L96

  ```python
  Settings._validate_redis_url(cls, value: str | None) -> str | None
  ```
- [`Settings._production_database`](../app/core/config.py#L100) — L100

  ```python
  Settings._production_database(self) -> 'Settings'
  ```
- [`Settings._redact_validation_inputs`](../app/core/config.py#L130) — L130

  ```python
  Settings._redact_validation_inputs(cls, value: Any, handler: ModelWrapValidatorHandler['Settings']) -> 'Settings'
  ```

</details>

### app/core/db.py

SQLAlchemy engine/session 工厂、UTC/隔离级别和存储租约依赖。

[打开源码](../app/core/db.py) · 58 行

主要可调用定义：

- [`create_engine`](../app/core/db.py#L14) — L14

  ```python
  create_engine(database_url: str, *, pool_size: int=5, max_overflow: int=5, pool_timeout: float=2) -> AsyncEngine
  ```
  Create an unconnected engine with explicit per-dialect session semantics.
- [`create_session_factory`](../app/core/db.py#L40) — L40

  ```python
  create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]
  ```
- [`get_session`](../app/core/db.py#L44) — L44

  ```python
  async get_session(request: Request) -> AsyncGenerator[AsyncSession, None]
  ```
  Pin storage through session cleanup; the request never auto-commits.

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`create_engine._foreign_keys`](../app/core/db.py#L30) — L30

  ```python
  create_engine._foreign_keys(connection: Any, _record: Any) -> None
  ```

</details>

### app/core/enums.py

主体、生命周期与决策枚举；存在历史预留不代表功能开放。

[打开源码](../app/core/enums.py) · 25 行

类：[`PrincipalType`](../app/core/enums.py#L4)；[`WorkLifecycle`](../app/core/enums.py#L10)；[`DecisionOutcome`](../app/core/enums.py#L19)。

### app/core/errors.py

统一脱敏 HTTP 错误映射与 request-id。

[打开源码](../app/core/errors.py) · 67 行

主要可调用定义：

- [`error_response`](../app/core/errors.py#L18) — L18

  ```python
  error_response(request: Request, status: int, code: str, message: str, headers: Mapping[str, str] | None=None) -> JSONResponse
  ```
- [`unexpected_error_response`](../app/core/errors.py#L36) — L36

  ```python
  unexpected_error_response(request: Request, exc: Exception) -> JSONResponse
  ```
- [`register_exception_handlers`](../app/core/errors.py#L46) — L46

  ```python
  register_exception_handlers(app: FastAPI) -> None
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`register_exception_handlers.validation_handler`](../app/core/errors.py#L47) — L47

  ```python
  async register_exception_handlers.validation_handler(request: Request, _exc: RequestValidationError) -> JSONResponse
  ```
- [`register_exception_handlers.http_handler`](../app/core/errors.py#L50) — L50

  ```python
  async register_exception_handlers.http_handler(request: Request, exc: HTTPException) -> JSONResponse
  ```
- [`register_exception_handlers.unexpected_handler`](../app/core/errors.py#L56) — L56

  ```python
  async register_exception_handlers.unexpected_handler(request: Request, exc: Exception) -> JSONResponse
  ```

</details>

### app/core/idempotency.py

早期幂等键值对象；当前持久幂等协议在 CommandKernel。

[打开源码](../app/core/idempotency.py) · 15 行

类：[`IdempotencyKey`](../app/core/idempotency.py#L10)。

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`IdempotencyKey.__post_init__`](../app/core/idempotency.py#L13) — L13

  ```python
  IdempotencyKey.__post_init__(self) -> None
  ```

</details>

### app/core/middleware.py

请求关联、异常边界与访问日志查询串脱敏。

[打开源码](../app/core/middleware.py) · 65 行

类：[`AccessQueryFilter`](../app/core/middleware.py#L16)；[`ResponseStreamError`](../app/core/middleware.py#L32)；[`RequestIdMiddleware`](../app/core/middleware.py#L36)。

主要可调用定义：

- [`AccessQueryFilter.filter`](../app/core/middleware.py#L19) — L19

  ```python
  AccessQueryFilter.filter(self, record: logging.LogRecord) -> bool
  ```
- [`install_safe_access_logging`](../app/core/middleware.py#L26) — L26

  ```python
  install_safe_access_logging() -> None
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`RequestIdMiddleware.__init__`](../app/core/middleware.py#L37) — L37

  ```python
  RequestIdMiddleware.__init__(self, app: ASGIApp) -> None
  ```
- [`RequestIdMiddleware.__call__`](../app/core/middleware.py#L40) — L40

  ```python
  async RequestIdMiddleware.__call__(self, scope: Scope, receive: Receive, send: Send) -> None
  ```
- [`RequestIdMiddleware.__call__.correlated_send`](../app/core/middleware.py#L49) — L49

  ```python
  async RequestIdMiddleware.__call__.correlated_send(message: Message) -> None
  ```

</details>

### app/core/outbox.py

早期消息值对象；持久投递实现位于 execution/delivery。

[打开源码](../app/core/outbox.py) · 20 行

类：[`OutboxMessage`](../app/core/outbox.py#L12)。

### app/core/redis.py

有连接数和超时限制的 Redis 客户端工厂。

[打开源码](../app/core/redis.py) · 38 行

主要可调用定义：

- [`create_redis_client`](../app/core/redis.py#L18) — L18

  ```python
  create_redis_client(config: Settings) -> Redis
  ```
  Build an unconnected async client with bounded connection and read waits.

### app/core/schemas.py

系统探针与错误公共响应 DTO。

[打开源码](../app/core/schemas.py) · 23 行

类：[`HealthResponse`](../app/core/schemas.py#L6)；[`ErrorDetail`](../app/core/schemas.py#L16)；[`ErrorResponse`](../app/core/schemas.py#L22)。

### app/core/time.py

UTC 时钟小工具。

[打开源码](../app/core/time.py) · 5 行

主要可调用定义：

- [`utcnow`](../app/core/time.py#L4) — L4

  ```python
  utcnow() -> datetime
  ```

### app/delivery/__init__.py

包初始化与公开符号导出。

[打开源码](../app/delivery/__init__.py) · 29 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/delivery/contracts.py

投递信封、回执、对账、sink 与当前投递授权协议。

[打开源码](../app/delivery/contracts.py) · 122 行

类：[`DeliveryEnvelope`](../app/delivery/contracts.py#L20)；[`DeliveryReceipt`](../app/delivery/contracts.py#L33)；[`SinkCapabilities`](../app/delivery/contracts.py#L42)；[`ReconciliationStatus`](../app/delivery/contracts.py#L54)；[`ReconciliationResult`](../app/delivery/contracts.py#L61)；[`DeliverySink`](../app/delivery/contracts.py#L66)；[`CurrentDeliveryAuthorizer`](../app/delivery/contracts.py#L78)；[`DeliveryRunSummary`](../app/delivery/contracts.py#L98)；[`DeliveryWorkerStatus`](../app/delivery/contracts.py#L108)；[`DeliveryDrainTimeout`](../app/delivery/contracts.py#L113)；[`DeliveryWorkerClosed`](../app/delivery/contracts.py#L121)。

主要可调用定义：

- [`DeliverySink.deliver`](../app/delivery/contracts.py#L69) — L69

  ```python
  async DeliverySink.deliver(self, envelope: DeliveryEnvelope) -> DeliveryReceipt
  ```
  Acknowledge delivery, or raise; ambiguous errors remain unknown.
- [`DeliverySink.reconcile`](../app/delivery/contracts.py#L73) — L73

  ```python
  async DeliverySink.reconcile(self, envelope: DeliveryEnvelope) -> ReconciliationResult
  ```
  Query the original stable delivery identity without sending again.
- [`CurrentDeliveryAuthorizer.authorize`](../app/delivery/contracts.py#L79) — L79

  ```python
  async CurrentDeliveryAuthorizer.authorize(self, session: AsyncSession, *, service_ref: str, envelope: DeliveryEnvelope, operation: Literal['send', 'reconcile'], request_id: str) -> AuthorizationDecision
  ```
  Load current trusted facts and call app.authz inside this transaction.

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`DeliveryDrainTimeout.__init__`](../app/delivery/contracts.py#L116) — L116

  ```python
  DeliveryDrainTimeout.__init__(self, pending_operations: int) -> None
  ```

</details>

### app/delivery/worker.py

短事务领取、事务外投递、持久租约/fencing、unknown 对账与关闭排空；不默认装配。

[打开源码](../app/delivery/worker.py) · 694 行

类：[`_Claim`](../app/delivery/worker.py#L50)；[`_Selection`](../app/delivery/worker.py#L59)；[`OutboxWorker`](../app/delivery/worker.py#L64)。

主要可调用定义：

- [`OutboxWorker.status`](../app/delivery/worker.py#L133) — L133

  ```python
  OutboxWorker.status(self) -> DeliveryWorkerStatus
  ```
  Public supervision snapshot; exception messages may contain secrets.
- [`OutboxWorker.run_once`](../app/delivery/worker.py#L466) — L466

  ```python
  async OutboxWorker.run_once(self, tenant_ref: str) -> DeliveryRunSummary
  ```
  Process at most the configured number of this tenant's pending rows.
- [`OutboxWorker.reconcile_one`](../app/delivery/worker.py#L485) — L485

  ```python
  async OutboxWorker.reconcile_one(self, tenant_ref: str, message_id: str) -> str
  ```
  Explicitly query an unknown message; only proven safe absence requeues.
- [`OutboxWorker.retry_blocked`](../app/delivery/worker.py#L533) — L533

  ```python
  async OutboxWorker.retry_blocked(self, tenant_ref: str, message_id: str) -> str
  ```
  Explicitly reopen a never-dispatched block after current authorization.
- [`OutboxWorker.start`](../app/delivery/worker.py#L576) — L576

  ```python
  OutboxWorker.start(self, tenant_refs: Sequence[str]) -> asyncio.Task[None]
  ```
  Start standalone polling and return its observable task.
- [`OutboxWorker.run`](../app/delivery/worker.py#L592) — L592

  ```python
  async OutboxWorker.run(self, tenant_refs: Sequence[str]) -> None
  ```
  Poll until stopped; a fault stops intake and propagates to the owner.
- [`OutboxWorker.request_stop`](../app/delivery/worker.py#L638) — L638

  ```python
  OutboxWorker.request_stop(self, *, cancel_operation: bool=False) -> None
  ```
  Cooperatively stop intake and cancel only active operation tasks.
- [`OutboxWorker.wait`](../app/delivery/worker.py#L653) — L653

  ```python
  async OutboxWorker.wait(self) -> None
  ```
  Observe polling completion/failure without cancelling its owner task.
- [`OutboxWorker.close`](../app/delivery/worker.py#L661) — L661

  ```python
  async OutboxWorker.close(self) -> None
  ```
  Stop intake and await owned I/O and durable cancellation bookkeeping.

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_utc_now`](../app/delivery/worker.py#L45) — L45

  ```python
  _utc_now() -> datetime
  ```
- [`OutboxWorker.__init__`](../app/delivery/worker.py#L74) — L74

  ```python
  OutboxWorker.__init__(self, session_factory: async_sessionmaker[AsyncSession], *, authorizer: CurrentDeliveryAuthorizer, sinks: Mapping[str, DeliverySink], service_ref: str, worker_ref: str, clock: Callable[[], datetime]=_utc_now, lease_duration: timedelta=timedelta(seconds=30), delivery_timeout: float=10, close_timeout: float=10, poll_interval: float=1, retry_delay: timedelta=timedelta(seconds=1), per_tenant_limit: int=10, max_send_attempts: int=3) -> None
  ```
- [`OutboxWorker._now`](../app/delivery/worker.py#L143) — L143

  ```python
  OutboxWorker._now(self) -> datetime
  ```
- [`OutboxWorker._operation`](../app/delivery/worker.py#L150) — L150

  ```python
  async OutboxWorker._operation(self) -> AsyncIterator[None]
  ```
- [`OutboxWorker._envelope`](../app/delivery/worker.py#L162) — L162

  ```python
  OutboxWorker._envelope(row: OutboxRecordTable) -> DeliveryEnvelope
  ```
- [`OutboxWorker._decision_matches`](../app/delivery/worker.py#L175) — L175

  ```python
  OutboxWorker._decision_matches(self, decision: AuthorizationDecision, envelope: DeliveryEnvelope, request_id: str) -> bool
  ```
- [`OutboxWorker._authorization_error`](../app/delivery/worker.py#L185) — L185

  ```python
  async OutboxWorker._authorization_error(self, uow: UnitOfWork, envelope: DeliveryEnvelope, operation: Operation, request_id: str) -> str | None
  ```
- [`OutboxWorker._send_count`](../app/delivery/worker.py#L206) — L206

  ```python
  async OutboxWorker._send_count(self, session: AsyncSession, envelope: DeliveryEnvelope) -> int
  ```
- [`OutboxWorker._recover_expired`](../app/delivery/worker.py#L220) — L220

  ```python
  async OutboxWorker._recover_expired(self, tenant_ref: str) -> int
  ```
- [`OutboxWorker._expire`](../app/delivery/worker.py#L245) — L245

  ```python
  async OutboxWorker._expire(self, session: AsyncSession, row: OutboxRecordTable, now: datetime) -> None
  ```
- [`OutboxWorker._claim`](../app/delivery/worker.py#L262) — L262

  ```python
  async OutboxWorker._claim(self, tenant_ref: str, *, operation: Operation, message_id: str | None=None) -> _Selection | None
  ```
- [`OutboxWorker._finish`](../app/delivery/worker.py#L336) — L336

  ```python
  async OutboxWorker._finish(self, claim: _Claim, *, status: Literal['sent', 'unknown', 'pending'], error_code: str | None=None, receipt: DeliveryReceipt | None=None, attempt_status: str | None=None) -> bool
  ```
- [`OutboxWorker._receipt_matches`](../app/delivery/worker.py#L413) — L413

  ```python
  OutboxWorker._receipt_matches(receipt: object, envelope: DeliveryEnvelope) -> bool
  ```
- [`OutboxWorker._send`](../app/delivery/worker.py#L429) — L429

  ```python
  async OutboxWorker._send(self, claim: _Claim) -> str
  ```
- [`OutboxWorker._finish_cancelled`](../app/delivery/worker.py#L457) — L457

  ```python
  async OutboxWorker._finish_cancelled(self, claim: _Claim, code: str, cancelled: asyncio.CancelledError) -> None
  ```

</details>

### app/execution/__init__.py

包初始化与公开符号导出。

[打开源码](../app/execution/__init__.py) · 1 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/execution/contracts.py

可信 ExecutionContext、命令/效果/结果 DTO、loader/coordinator/validator 接口。

[打开源码](../app/execution/contracts.py) · 216 行

类：[`ExecutionContext`](../app/execution/contracts.py#L45)；[`CommandEnvelope`](../app/execution/contracts.py#L93)；[`OutboxIntent`](../app/execution/contracts.py#L111)；[`CommandEffect`](../app/execution/contracts.py#L131)；[`CommandResult`](../app/execution/contracts.py#L139)；[`AuthorizationLockCoordinator`](../app/execution/contracts.py#L147)；[`CommandIdentityValidator`](../app/execution/contracts.py#L157)；[`AuthorizationLoader`](../app/execution/contracts.py#L168)；[`CommandSpec`](../app/execution/contracts.py#L191)。

主要可调用定义：

- [`utc_clock`](../app/execution/contracts.py#L24) — L24

  ```python
  utc_clock() -> datetime
  ```
- [`require_ref`](../app/execution/contracts.py#L28) — L28

  ```python
  require_ref(value: str, maximum: int) -> None
  ```
- [`require_time`](../app/execution/contracts.py#L39) — L39

  ```python
  require_time(value: datetime) -> None
  ```
- [`ExecutionContext.check_live`](../app/execution/contracts.py#L82) — L82

  ```python
  ExecutionContext.check_live(self, now: datetime) -> None
  ```
- [`AuthorizationLockCoordinator.acquire_locks`](../app/execution/contracts.py#L148) — L148

  ```python
  async AuthorizationLockCoordinator.acquire_locks(self, session: AsyncSession, context: ExecutionContext) -> None
  ```
  Acquire trust/global identity locks before the organization guard.
- [`CommandIdentityValidator.validate_before_commit`](../app/execution/contracts.py#L158) — L158

  ```python
  async CommandIdentityValidator.validate_before_commit(self, session: AsyncSession, context: ExecutionContext, now: datetime) -> None
  ```
  Revalidate server-issued authentication evidence with no network or commit.
- [`AuthorizationLoader.load`](../app/execution/contracts.py#L169) — L169

  ```python
  async AuthorizationLoader.load(self, session: AsyncSession, context: ExecutionContext, *, action: str, target_ref: str, expected_version: int | None, now: datetime) -> AuthorizationContext
  ```
  Read authoritative tenant-scoped facts using the current transaction.

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`ExecutionContext.__post_init__`](../app/execution/contracts.py#L57) — L57

  ```python
  ExecutionContext.__post_init__(self) -> None
  ```
- [`CommandEnvelope.__post_init__`](../app/execution/contracts.py#L101) — L101

  ```python
  CommandEnvelope.__post_init__(self) -> None
  ```
- [`OutboxIntent.__post_init__`](../app/execution/contracts.py#L119) — L119

  ```python
  OutboxIntent.__post_init__(self) -> None
  ```
- [`CommandSpec.__post_init__`](../app/execution/contracts.py#L200) — L200

  ```python
  CommandSpec.__post_init__(self) -> None
  ```

</details>

### app/execution/encoding.py

有界 JSON 规范化、复制与摘要，支撑幂等与审计。

[打开源码](../app/execution/encoding.py) · 75 行

主要可调用定义：

- [`json_bytes`](../app/execution/encoding.py#L57) — L57

  ```python
  json_bytes(value: Any) -> bytes
  ```
- [`copied_object`](../app/execution/encoding.py#L68) — L68

  ```python
  copied_object(value: Any) -> dict[str, Any]
  ```
- [`digest`](../app/execution/encoding.py#L74) — L74

  ```python
  digest(value: Any) -> str
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_validate`](../app/execution/encoding.py#L15) — L15

  ```python
  _validate(value: Any, budget: list[int], depth: int=0) -> None
  ```

</details>

### app/execution/errors.py

版本冲突、授权拒绝、截止、提交未知和事务所有权异常。

[打开源码](../app/execution/errors.py) · 60 行

类：[`ExecutionError`](../app/execution/errors.py#L4)；[`InvalidCommand`](../app/execution/errors.py#L11)；[`UnknownCommand`](../app/execution/errors.py#L15)；[`AuthorizationDenied`](../app/execution/errors.py#L19)；[`ObjectVersionConflict`](../app/execution/errors.py#L23)；[`ContextExpired`](../app/execution/errors.py#L27)；[`RequestDeadlineExceeded`](../app/execution/errors.py#L31)；[`TenantNotProvisioned`](../app/execution/errors.py#L35)；[`IdempotencyConflict`](../app/execution/errors.py#L39)；[`ExecutionNotFound`](../app/execution/errors.py#L43)；[`TransactionOwnershipError`](../app/execution/errors.py#L47)；[`CommitOutcomeUnknown`](../app/execution/errors.py#L51)；[`ExecutionCleanupFailed`](../app/execution/errors.py#L59)。

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`ExecutionError.__init__`](../app/execution/errors.py#L7) — L7

  ```python
  ExecutionError.__init__(self) -> None
  ```
- [`CommitOutcomeUnknown.__init__`](../app/execution/errors.py#L54) — L54

  ```python
  CommitOutcomeUnknown.__init__(self, execution_id: str) -> None
  ```

</details>

### app/execution/kernel.py

唯一命令协调者：当前授权、幂等/版本、领域 handler、结果/审计/outbox 原子提交与安全结果恢复。

[打开源码](../app/execution/kernel.py) · 434 行

类：[`CommandKernel`](../app/execution/kernel.py#L49)。

主要可调用定义：

- [`CommandKernel.execute`](../app/execution/kernel.py#L153) — L153

  ```python
  async CommandKernel.execute(self, context: ExecutionContext, command: CommandEnvelope) -> CommandResult
  ```
- [`CommandKernel.get_result`](../app/execution/kernel.py#L303) — L303

  ```python
  async CommandKernel.get_result(self, context: ExecutionContext, execution_id: str) -> CommandResult
  ```
  Current authorized read for the same tenant and authenticated actor.
- [`CommandKernel.read`](../app/execution/kernel.py#L333) — L333

  ```python
  async CommandKernel.read(self, context: ExecutionContext, *, action: str, target_ref: str, reader: Callable[[AsyncSession], Awaitable[T]]) -> T
  ```
  Load a value under current authorization and recheck before disclosure.
- [`CommandKernel.get_result_by_key`](../app/execution/kernel.py#L365) — L365

  ```python
  async CommandKernel.get_result_by_key(self, context: ExecutionContext, *, command_name: str, command_version: str, idempotency_key: str, target_ref: str) -> CommandResult
  ```
  Recover an uncertain HTTP result using the caller's original operation key.

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`CommandKernel.__init__`](../app/execution/kernel.py#L50) — L50

  ```python
  CommandKernel.__init__(self, sessions: async_sessionmaker[AsyncSession], *, loader: AuthorizationLoader, commands: Sequence[CommandSpec], clock: Clock=utc_clock, coordinator: AuthorizationLockCoordinator | None=None, identity_validator: CommandIdentityValidator | None=None) -> None
  ```
- [`CommandKernel._spec`](../app/execution/kernel.py#L74) — L74

  ```python
  CommandKernel._spec(self, name: str, version: str) -> CommandSpec
  ```
- [`CommandKernel._authorize`](../app/execution/kernel.py#L80) — L80

  ```python
  async CommandKernel._authorize(self, session: AsyncSession, context: ExecutionContext, *, action: str, target_ref: str, expected_version: int | None) -> tuple[AuthorizationDecision, AuthorizationContext]
  ```
- [`CommandKernel._result`](../app/execution/kernel.py#L146) — L146

  ```python
  CommandKernel._result(row: CommandExecutionTable, *, replayed: bool) -> CommandResult
  ```
- [`CommandKernel._check_identity_integration`](../app/execution/kernel.py#L402) — L402

  ```python
  CommandKernel._check_identity_integration(self, context: ExecutionContext) -> None
  ```
- [`CommandKernel._authorization_evidence`](../app/execution/kernel.py#L407) — L407

  ```python
  CommandKernel._authorization_evidence(facts: AuthorizationContext, decision: AuthorizationDecision, context: ExecutionContext) -> dict[str, object] | None
  ```

</details>

### app/execution/storage.py

受管理 UoW、事务所有权保护和 PostgreSQL 组织 guard。

[打开源码](../app/execution/storage.py) · 173 行

类：[`UnitOfWork`](../app/execution/storage.py#L102)。

主要可调用定义：

- [`managed_uow`](../app/execution/storage.py#L43) — L43

  ```python
  async managed_uow(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator['UnitOfWork']
  ```
  Always attempt rollback and close, without hiding an uncertain commit.
- [`lock_tenant`](../app/execution/storage.py#L79) — L79

  ```python
  async lock_tenant(session: AsyncSession, tenant_ref: str) -> None
  ```
  Lock an explicitly provisioned tenant; never create one from caller input.
- [`UnitOfWork.assert_owned`](../app/execution/storage.py#L143) — L143

  ```python
  UnitOfWork.assert_owned(self) -> None
  ```
- [`UnitOfWork.flush`](../app/execution/storage.py#L149) — L149

  ```python
  async UnitOfWork.flush(self) -> None
  ```
- [`UnitOfWork.commit`](../app/execution/storage.py#L153) — L153

  ```python
  async UnitOfWork.commit(self) -> None
  ```
- [`UnitOfWork.detach`](../app/execution/storage.py#L165) — L165

  ```python
  UnitOfWork.detach(self) -> None
  ```
- [`UnitOfWork.add`](../app/execution/storage.py#L171) — L171

  ```python
  UnitOfWork.add(self, instance: Any) -> None
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_finish_cleanup`](../app/execution/storage.py#L20) — L20

  ```python
  async _finish_cleanup(operation: Any) -> None
  ```
  Finish session cleanup even when the owner task was cancelled.
- [`UnitOfWork.__init__`](../app/execution/storage.py#L109) — L109

  ```python
  UnitOfWork.__init__(self, session: AsyncSession, connection: Connection) -> None
  ```
- [`UnitOfWork._before_commit`](../app/execution/storage.py#L124) — L124

  ```python
  UnitOfWork._before_commit(self, _session: Session) -> None
  ```
- [`UnitOfWork._after_rollback`](../app/execution/storage.py#L129) — L129

  ```python
  UnitOfWork._after_rollback(self, _session: Session) -> None
  ```
- [`UnitOfWork._before_connection_commit`](../app/execution/storage.py#L132) — L132

  ```python
  UnitOfWork._before_connection_commit(self, _connection: Connection) -> None
  ```
- [`UnitOfWork._before_connection_rollback`](../app/execution/storage.py#L137) — L137

  ```python
  UnitOfWork._before_connection_rollback(self, _connection: Connection) -> None
  ```
- [`UnitOfWork._commit_by_owner`](../app/execution/storage.py#L157) — L157

  ```python
  async UnitOfWork._commit_by_owner(self) -> None
  ```

</details>

### app/features/__init__.py

包初始化与公开符号导出。

[打开源码](../app/features/__init__.py) · 1 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/features/work/__init__.py

包初始化与公开符号导出。

[打开源码](../app/features/work/__init__.py) · 1 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/features/work/commands.py

历史 ClaimWork/SubmitDelivery/AcceptDelivery DTO 草图；没有对应已注册命令或 HTTP 端点。

[打开源码](../app/features/work/commands.py) · 40 行

类：[`ClaimWork`](../app/features/work/commands.py#L12)；[`SubmitDelivery`](../app/features/work/commands.py#L23)；[`AcceptDelivery`](../app/features/work/commands.py#L34)。

### app/features/work/details.py

当前唯一默认领域写操作 work.details.update；只修改已有工作项的标题/说明与版本。

[打开源码](../app/features/work/details.py) · 120 行

类：[`WorkDetailsInput`](../app/features/work/details.py#L23)；[`WorkDetailsResult`](../app/features/work/details.py#L42)；[`WorkDetailsConflict`](../app/features/work/details.py#L53)。

主要可调用定义：

- [`WorkDetailsInput.validate_text`](../app/features/work/details.py#L34) — L34

  ```python
  WorkDetailsInput.validate_text(cls, value: str | None) -> str | None
  ```
- [`update_work_details`](../app/features/work/details.py#L80) — L80

  ```python
  async update_work_details(uow: UnitOfWork, _context: ExecutionContext, command: CommandEnvelope, payload: BaseModel) -> CommandEffect
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_work_id`](../app/features/work/details.py#L57) — L57

  ```python
  _work_id(target_ref: str) -> int
  ```
- [`_projection`](../app/features/work/details.py#L69) — L69

  ```python
  _projection(work: WorkItemTable) -> dict[str, str | int | None]
  ```

</details>

### app/identity/__init__.py

包初始化与公开符号导出。

[打开源码](../app/identity/__init__.py) · 1 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/identity/administration.py

dev/test 身份 bootstrap 与版本化撤销；共享管理协议移至 app.control。

[打开源码](../app/identity/administration.py) · 574 行

类：[`ProfilePlan`](../app/identity/administration.py#L88)；[`IdentityPlan`](../app/identity/administration.py#L96)；[`BootstrapPlan`](../app/identity/administration.py#L121)；[`RevocationRequest`](../app/identity/administration.py#L192)。

主要可调用定义：

- [`BootstrapPlan.normalize_organization_ref`](../app/identity/administration.py#L135) — L135

  ```python
  BootstrapPlan.normalize_organization_ref(cls, values: Any) -> Any
  ```
- [`BootstrapPlan.digest_payload`](../app/identity/administration.py#L138) — L138

  ```python
  BootstrapPlan.digest_payload(self) -> dict[str, Any]
  ```
- [`BootstrapPlan.validate_links`](../app/identity/administration.py#L146) — L146

  ```python
  BootstrapPlan.validate_links(self) -> Self
  ```
- [`RevocationRequest.normalize_legacy_input`](../app/identity/administration.py#L202) — L202

  ```python
  RevocationRequest.normalize_legacy_input(cls, values: Any) -> Any
  ```
- [`RevocationRequest.validate_target`](../app/identity/administration.py#L209) — L209

  ```python
  RevocationRequest.validate_target(self) -> Self
  ```
- [`RevocationRequest.journal_kind`](../app/identity/administration.py#L217) — L217

  ```python
  RevocationRequest.journal_kind(self) -> str
  ```
- [`RevocationRequest.digest_payload`](../app/identity/administration.py#L221) — L221

  ```python
  RevocationRequest.digest_payload(self) -> dict[str, Any]
  ```
- [`bootstrap`](../app/identity/administration.py#L228) — L228

  ```python
  async bootstrap(sessions: async_sessionmaker[AsyncSession], plan: BootstrapPlan, *, actor_ref: str, idempotency_key: str) -> AdministrationResult
  ```
  Install one explicit development/test identity foundation, with no business grants.
- [`revoke`](../app/identity/administration.py#L414) — L414

  ```python
  async revoke(sessions: async_sessionmaker[AsyncSession], request: RevocationRequest, *, actor_ref: str, idempotency_key: str) -> AdministrationResult
  ```
  Terminal local revocation with expected version, current row locks and atomic journal.

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_organization_input`](../app/identity/administration.py#L108) — L108

  ```python
  _organization_input(values: Any) -> Any
  ```
  Accept old operator input without exposing two mutable model fields.
- [`_install`](../app/identity/administration.py#L285) — L285

  ```python
  async _install(session: AsyncSession, plan: BootstrapPlan, actor_ref: str, idempotency_key: str, digest: str) -> AdministrationResult
  ```
- [`_state_snapshot`](../app/identity/administration.py#L486) — L486

  ```python
  async _state_snapshot(session: AsyncSession, row: PrincipalTable | OrganizationRootTable | OrganizationMembershipTable | AuthenticationProfileTable) -> dict[str, Any]
  ```
- [`_lock_target`](../app/identity/administration.py#L514) — L514

  ```python
  async _lock_target(session: AsyncSession, request: RevocationRequest) -> PrincipalTable | OrganizationRootTable | OrganizationMembershipTable | AuthenticationProfileTable
  ```

</details>

### app/identity/assurance.py

组织登录 MFA 策略及精确 profile/version 强度规则。

[打开源码](../app/identity/assurance.py) · 54 行

类：[`HumanAssuranceRule`](../app/identity/assurance.py#L23)；[`HumanAssurancePolicy`](../app/identity/assurance.py#L38)。

主要可调用定义：

- [`HumanAssuranceRule.unique_methods`](../app/identity/assurance.py#L32) — L32

  ```python
  HumanAssuranceRule.unique_methods(self) -> Self
  ```
- [`HumanAssurancePolicy.coherent_policy`](../app/identity/assurance.py#L46) — L46

  ```python
  HumanAssurancePolicy.coherent_policy(self) -> Self
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_valid_unicode`](../app/identity/assurance.py#L8) — L8

  ```python
  _valid_unicode(value: str) -> str
  ```

</details>

### app/identity/assurance_administration.py

受控变更组织 MFA 策略，预期版本与组织 epoch 协调。

[打开源码](../app/identity/assurance_administration.py) · 216 行

类：[`ConfigureHumanAuthenticationRequest`](../app/identity/assurance_administration.py#L47)。

主要可调用定义：

- [`configure_human_authentication`](../app/identity/assurance_administration.py#L133) — L133

  ```python
  async configure_human_authentication(sessions: async_sessionmaker[AsyncSession], request: ConfigureHumanAuthenticationRequest, *, actor_ref: str, idempotency_key: str) -> AdministrationResult
  ```
  Versioned update + organization epoch + recoverable journal in one transaction.

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_lock_rule_trusts`](../app/identity/assurance_administration.py#L54) — L54

  ```python
  async _lock_rule_trusts(session: AsyncSession, rules: tuple[HumanAssuranceRule, ...]) -> None
  ```
  Keep trust -> organization lock order shared with authenticated requests.
- [`_validate_rules`](../app/identity/assurance_administration.py#L71) — L71

  ```python
  async _validate_rules(session: AsyncSession, request: ConfigureHumanAuthenticationRequest, now: datetime) -> None
  ```
- [`_snapshot`](../app/identity/assurance_administration.py#L122) — L122

  ```python
  _snapshot(row: OrganizationHumanAuthenticationPolicyTable | None) -> dict[str, Any]
  ```

</details>

### app/identity/assurance_verification.py

校验当前 profile、ACR/AMR、auth_time 和策略版本/摘要。

[打开源码](../app/identity/assurance_verification.py) · 83 行

类：[`CurrentHumanAssurance`](../app/identity/assurance_verification.py#L22)。

主要可调用定义：

- [`verify_human_assurance`](../app/identity/assurance_verification.py#L28) — L28

  ```python
  async verify_human_assurance(session: AsyncSession, credential: VerifiedCredential, organization_ref: str, now: datetime, *, expected_version: int | None=None, expected_digest: str | None=None) -> CurrentHumanAssurance
  ```
  Caller holds the organization guard; no network, commit or cached allow.

### app/identity/authentication.py

中立 AccessTokenProfile、JWKS/签名验证与 VerifiedCredential；保留 KeycloakProfile 别名。

[打开源码](../app/identity/authentication.py) · 309 行

类：[`AuthenticationRejected`](../app/identity/authentication.py#L20)；[`AuthenticationUnavailable`](../app/identity/authentication.py#L28)；[`AuthenticationAssuranceRequired`](../app/identity/authentication.py#L36)；[`AccessTokenProfile`](../app/identity/authentication.py#L44)；[`VerifiedCredential`](../app/identity/authentication.py#L88)；[`KeycloakAuthenticator`](../app/identity/authentication.py#L113)。

主要可调用定义：

- [`KeycloakAuthenticator.aclose`](../app/identity/authentication.py#L146) — L146

  ```python
  async KeycloakAuthenticator.aclose(self) -> None
  ```
- [`KeycloakAuthenticator.authenticate`](../app/identity/authentication.py#L150) — L150

  ```python
  async KeycloakAuthenticator.authenticate(self, access_token: str) -> VerifiedCredential
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`AuthenticationRejected.__init__`](../app/identity/authentication.py#L23) — L23

  ```python
  AuthenticationRejected.__init__(self, code: str='invalid_credential') -> None
  ```
- [`AuthenticationUnavailable.__init__`](../app/identity/authentication.py#L31) — L31

  ```python
  AuthenticationUnavailable.__init__(self, code: str='authentication_unavailable') -> None
  ```
- [`AuthenticationAssuranceRequired.__init__`](../app/identity/authentication.py#L39) — L39

  ```python
  AuthenticationAssuranceRequired.__init__(self) -> None
  ```
- [`AccessTokenProfile.__post_init__`](../app/identity/authentication.py#L60) — L60

  ```python
  AccessTokenProfile.__post_init__(self) -> None
  ```
- [`KeycloakAuthenticator.__init__`](../app/identity/authentication.py#L121) — L121

  ```python
  KeycloakAuthenticator.__init__(self, profile: AccessTokenProfile, *, http_client: httpx.AsyncClient | None=None, jwks_cache_seconds: float=60, unknown_kid_refresh_seconds: float=5) -> None
  ```
- [`KeycloakAuthenticator._get_key`](../app/identity/authentication.py#L238) — L238

  ```python
  async KeycloakAuthenticator._get_key(self, kid: str) -> RSAPublicKey
  ```
- [`KeycloakAuthenticator._fetch_keys`](../app/identity/authentication.py#L258) — L258

  ```python
  async KeycloakAuthenticator._fetch_keys(self) -> dict[str, RSAPublicKey]
  ```
- [`_bounded_string`](../app/identity/authentication.py#L298) — L298

  ```python
  _bounded_string(value: Any, limit: int) -> str
  ```
- [`_numeric_date`](../app/identity/authentication.py#L306) — L306

  ```python
  _numeric_date(value: Any) -> datetime
  ```

</details>

### app/identity/evidence.py

服务器生成的短期人员认证证据与审计白名单；不是可提交的请求 DTO。

[打开源码](../app/identity/evidence.py) · 73 行

类：[`HumanAuthenticationEvidence`](../app/identity/evidence.py#L11)。

主要可调用定义：

- [`HumanAuthenticationEvidence.audit_snapshot`](../app/identity/evidence.py#L40) — L40

  ```python
  HumanAuthenticationEvidence.audit_snapshot(self) -> dict[str, Any]
  ```
  Explicit audit allowlist excludes bearer tokens and external subjects.

### app/identity/native.py

原生 access JWT adapter，当前账号/会话事实与 Principal 映射重验。

[打开源码](../app/identity/native.py) · 108 行

类：[`NativeAuthenticator`](../app/identity/native.py#L25)。

主要可调用定义：

- [`NativeAuthenticator.authenticate`](../app/identity/native.py#L30) — L30

  ```python
  async NativeAuthenticator.authenticate(self, access_token: str) -> VerifiedCredential
  ```
- [`revalidate_native_credential`](../app/identity/native.py#L81) — L81

  ```python
  async revalidate_native_credential(session: AsyncSession, credential: VerifiedCredential, principal_ref: str, now: datetime) -> None
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`NativeAuthenticator.__init__`](../app/identity/native.py#L26) — L26

  ```python
  NativeAuthenticator.__init__(self, profile: AccessTokenProfile, keyring: KeyRing) -> None
  ```

</details>

### app/identity/service.py

发行来源/trust/profile→精确账号映射→单组织成员，签发短期 evidence 并提供锁内重验。

[打开源码](../app/identity/service.py) · 573 行

类：[`IdentityDenied`](../app/identity/service.py#L49)；[`ProfileConfiguration`](../app/identity/service.py#L53)；[`AdmissionConstraints`](../app/identity/service.py#L62)；[`SelfIdentity`](../app/identity/service.py#L69)；[`VerificationSnapshot`](../app/identity/service.py#L97)；[`IdentityService`](../app/identity/service.py#L118)。

主要可调用定义：

- [`content_digest`](../app/identity/service.py#L79) — L79

  ```python
  content_digest(value: Any) -> str
  ```
- [`profile_content`](../app/identity/service.py#L83) — L83

  ```python
  profile_content(row: AuthenticationProfileTable) -> dict[str, Any]
  ```
- [`current`](../app/identity/service.py#L105) — L105

  ```python
  current(row: Any, now: datetime) -> bool
  ```
- [`IdentityService.aclose`](../app/identity/service.py#L145) — L145

  ```python
  async IdentityService.aclose(self) -> None
  ```
- [`IdentityService.read_self`](../app/identity/service.py#L206) — L206

  ```python
  async IdentityService.read_self(self, access_token: str, *, profile_ref: str, profile_version: int) -> SelfIdentity
  ```
- [`IdentityService.authenticate_human`](../app/identity/service.py#L223) — L223

  ```python
  async IdentityService.authenticate_human(self, access_token: str, *, profile_ref: str, profile_version: int) -> HumanAuthenticationEvidence
  ```
- [`IdentityService.acquire_human_locks`](../app/identity/service.py#L425) — L425

  ```python
  async IdentityService.acquire_human_locks(self, session: AsyncSession, evidence: HumanAuthenticationEvidence) -> None
  ```
  Caller owns the transaction; organization guard must be acquired next.
- [`IdentityService.validate_human`](../app/identity/service.py#L446) — L446

  ```python
  async IdentityService.validate_human(self, session: AsyncSession, evidence: HumanAuthenticationEvidence, now: datetime) -> None
  ```
  Revalidate exact evidence under trust, principal and organization locks.

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`IdentityService.__init__`](../app/identity/service.py#L119) — L119

  ```python
  IdentityService.__init__(self, sessions: async_sessionmaker[AsyncSession], *, environment: str, http_client: httpx.AsyncClient | None=None, request_timeout_seconds: float=10, native_keyring: KeyRing | None=None, native_issuer: str | None=None) -> None
  ```
- [`IdentityService._snapshot`](../app/identity/service.py#L149) — L149

  ```python
  async IdentityService._snapshot(self, session: AsyncSession, profile_ref: str, profile_version: int) -> VerificationSnapshot
  ```
- [`IdentityService._authenticate_identity`](../app/identity/service.py#L240) — L240

  ```python
  async IdentityService._authenticate_identity(self, access_token: str, *, profile_ref: str, profile_version: int, require_human: bool=False) -> tuple[SelfIdentity, HumanAuthenticationEvidence | None]
  ```
- [`IdentityService._workload`](../app/identity/service.py#L546) — L546

  ```python
  async IdentityService._workload(self, session: AsyncSession, credential: VerifiedCredential, external_ref: str, principal_ref: str, now: datetime) -> WorkloadRegistrationTable | None
  ```

</details>

### app/main.py

FastAPI 工厂、lifespan、路由与错误/安全中间件挂载。

[打开源码](../app/main.py) · 74 行

主要可调用定义：

- [`create_app`](../app/main.py#L21) — L21

  ```python
  create_app(config: Settings | None=None) -> FastAPI
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`create_app.lifespan`](../app/main.py#L26) — L26

  ```python
  async create_app.lifespan(application: FastAPI) -> AsyncIterator[None]
  ```

</details>

### app/models/__init__.py

包初始化与公开符号导出。

[打开源码](../app/models/__init__.py) · 1 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/models/authorization.py

8 张动作、角色、scope、资源、任职、关系物理表。

[打开源码](../app/models/authorization.py) · 368 行

类：[`PermissionActionTable`](../app/models/authorization.py#L27)；[`RoleDefinitionTable`](../app/models/authorization.py#L44)；[`RoleVersionTable`](../app/models/authorization.py#L67)；[`RoleVersionActionTable`](../app/models/authorization.py#L95)；[`AuthorizationScopeTable`](../app/models/authorization.py#L115)；[`AuthorizationResourceTable`](../app/models/authorization.py#L146)；[`RoleAssignmentTable`](../app/models/authorization.py#L176)；[`RelationTupleTable`](../app/models/authorization.py#L275)。

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_bound_or_legacy`](../app/models/authorization.py#L19) — L19

  ```python
  _bound_or_legacy(*columns: str, name: str) -> CheckConstraint
  ```
  Allow an untouched historical row, or require all binding columns.

</details>

### app/models/base.py

SQLAlchemy Base、主键与时间列 mixin。

[打开源码](../app/models/base.py) · 21 行

类：[`Base`](../app/models/base.py#L9)；[`IdMixin`](../app/models/base.py#L13)；[`TimestampMixin`](../app/models/base.py#L17)。

### app/models/execution.py

5 张命令、审计、outbox、attempt、guard 表。

[打开源码](../app/models/execution.py) · 184 行

类：[`TenantGuardTable`](../app/models/execution.py#L13)；[`CommandExecutionTable`](../app/models/execution.py#L24)；[`AuditRecordTable`](../app/models/execution.py#L71)；[`OutboxRecordTable`](../app/models/execution.py#L106)；[`DeliveryAttemptTable`](../app/models/execution.py#L152)。

### app/models/identity.py

Principal 人员/服务主体表。

[打开源码](../app/models/identity.py) · 27 行

类：[`PrincipalTable`](../app/models/identity.py#L10)。

### app/models/identity_foundation.py

身份来源/trust/profile、组织/成员、MFA、workload、bootstrap/journal 等 11 张表及 mixin。

[打开源码](../app/models/identity_foundation.py) · 359 行

类：[`VersionMixin`](../app/models/identity_foundation.py#L33)；[`StatusMixin`](../app/models/identity_foundation.py#L37)；[`ValidityMixin`](../app/models/identity_foundation.py#L41)；[`TrustStateTable`](../app/models/identity_foundation.py#L46)；[`IdentityProviderTable`](../app/models/identity_foundation.py#L63)；[`AuthenticationProfileTable`](../app/models/identity_foundation.py#L80)；[`OrganizationRootTable`](../app/models/identity_foundation.py#L106)；[`OrganizationAuthenticationBindingTable`](../app/models/identity_foundation.py#L123)；[`OrganizationHumanAuthenticationPolicyTable`](../app/models/identity_foundation.py#L158)；[`ExternalIdentityTable`](../app/models/identity_foundation.py#L183)；[`OrganizationMembershipTable`](../app/models/identity_foundation.py#L217)；[`WorkloadRegistrationTable`](../app/models/identity_foundation.py#L257)；[`IdentityControlOperationTable`](../app/models/identity_foundation.py#L306)；[`BootstrapRecordTable`](../app/models/identity_foundation.py#L337)。

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_json`](../app/models/identity_foundation.py#L29) — L29

  ```python
  _json() -> JSON
  ```

</details>

### app/models/native_auth.py

11 张原生凭据、客户端、登录/RP/SSO、同意、退出、code/refresh、安全事件和限流表。

[打开源码](../app/models/native_auth.py) · 209 行

类：[`NativeAccountTable`](../app/models/native_auth.py#L14)；[`NativeClientTable`](../app/models/native_auth.py#L47)；[`NativeBrowserSessionTable`](../app/models/native_auth.py#L73)；[`NativeConsentTable`](../app/models/native_auth.py#L85)；[`NativeLogoutRequestTable`](../app/models/native_auth.py#L96)；[`NativeLoginRequestTable`](../app/models/native_auth.py#L111)；[`NativeSessionTable`](../app/models/native_auth.py#L148)；[`NativeAuthorizationCodeTable`](../app/models/native_auth.py#L174)；[`NativeRefreshTokenTable`](../app/models/native_auth.py#L184)；[`NativeAuthEventTable`](../app/models/native_auth.py#L194)；[`NativeLoginThrottleTable`](../app/models/native_auth.py#L203)。

### app/models/work.py

当前最小工作项事实表。

[打开源码](../app/models/work.py) · 22 行

类：[`WorkItemTable`](../app/models/work.py#L8)。

### app/native_auth/__init__.py

包初始化与公开符号导出。

[打开源码](../app/native_auth/__init__.py) · 1 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/native_auth/administration.py

将本地账号显式绑定既有 HUMAN 与成员经历；安装不授业务权限。

[打开源码](../app/native_auth/administration.py) · 277 行

类：[`InstallPlan`](../app/native_auth/administration.py#L53)。

主要可调用定义：

- [`InstallPlan.validate_plan`](../app/native_auth/administration.py#L67) — L67

  ```python
  InstallPlan.validate_plan(self) -> Self
  ```
- [`InstallPlan.digest_payload`](../app/native_auth/administration.py#L79) — L79

  ```python
  InstallPlan.digest_payload(self, key: bytes) -> dict[str, Any]
  ```
- [`install`](../app/native_auth/administration.py#L107) — L107

  ```python
  async install(sessions: async_sessionmaker[AsyncSession], plan: InstallPlan, *, actor_ref: str, idempotency_key: str, credential_digest_key: bytes) -> AdministrationResult
  ```
  Bind a new native account to a verified existing membership, with no grants.

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_profile`](../app/native_auth/administration.py#L90) — L90

  ```python
  _profile(plan: InstallPlan) -> AuthenticationProfileTable
  ```
- [`_install`](../app/native_auth/administration.py#L134) — L134

  ```python
  async _install(session: AsyncSession, plan: InstallPlan, encoded_password: str, actor_ref: str, idempotency_key: str, digest: str) -> AdministrationResult
  ```

</details>

### app/native_auth/browser.py

浏览器 SSO 复用与用户同意，保持原认证时间、当前策略期限和固定授权请求。

[打开源码](../app/native_auth/browser.py) · 252 行

类：[`BrowserOutcome`](../app/native_auth/browser.py#L26)；[`BrowserAuthorization`](../app/native_auth/browser.py#L35)。

主要可调用定义：

- [`BrowserAuthorization.resume`](../app/native_auth/browser.py#L73) — L73

  ```python
  async BrowserAuthorization.resume(self, request_ref: str, browser_secret: str, csrf_token: str, sso_cookie: str | None=None) -> bool
  ```
  Only a live opaque browser cookie can reuse the original MFA time.
- [`BrowserAuthorization.advance`](../app/native_auth/browser.py#L140) — L140

  ```python
  async BrowserAuthorization.advance(self, request_ref: str, browser_secret: str, csrf_token: str, consent: bool | None=None) -> BrowserOutcome
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`BrowserAuthorization.__init__`](../app/native_auth/browser.py#L36) — L36

  ```python
  BrowserAuthorization.__init__(self, sessions: async_sessionmaker[AsyncSession], config: NativeAuthConfig, *, credentials: NativeCredentials, oauth: NativeOAuth) -> None
  ```
- [`BrowserAuthorization._client`](../app/native_auth/browser.py#L49) — L49

  ```python
  async BrowserAuthorization._client(self, session: AsyncSession, request: NativeLoginRequestTable) -> NativeClientTable
  ```
- [`BrowserAuthorization._error`](../app/native_auth/browser.py#L65) — L65

  ```python
  BrowserAuthorization._error(self, request: NativeLoginRequestTable, error: str) -> OAuthError
  ```

</details>

### app/native_auth/client_administration.py

受控多客户端配置/轮换/禁用及独立 Stage 业务来源准入。

[打开源码](../app/native_auth/client_administration.py) · 432 行

类：[`ClientPlan`](../app/native_auth/client_administration.py#L46)；[`ConfigureClientPlan`](../app/native_auth/client_administration.py#L58)；[`RotateClientSecretPlan`](../app/native_auth/client_administration.py#L84)；[`AdmitClientPlan`](../app/native_auth/client_administration.py#L88)。

主要可调用定义：

- [`ClientPlan.valid_references`](../app/native_auth/client_administration.py#L52) — L52

  ```python
  ClientPlan.valid_references(self) -> Self
  ```
- [`ConfigureClientPlan.valid_metadata`](../app/native_auth/client_administration.py#L71) — L71

  ```python
  ConfigureClientPlan.valid_metadata(self) -> Self
  ```
- [`AdmitClientPlan.valid_admission_references`](../app/native_auth/client_administration.py#L97) — L97

  ```python
  AdmitClientPlan.valid_admission_references(self) -> Self
  ```
- [`client_snapshot`](../app/native_auth/client_administration.py#L120) — L120

  ```python
  client_snapshot(client: NativeClientTable) -> dict[str, Any]
  ```
  Safe metadata for inspection and journals; never include a secret or its digest.
- [`configure_client`](../app/native_auth/client_administration.py#L173) — L173

  ```python
  async configure_client(sessions: async_sessionmaker[AsyncSession], plan: ConfigureClientPlan, *, actor_ref: str, idempotency_key: str, client_secret_hash: str | None=None) -> AdministrationResult
  ```
  Create or fully replace registered metadata, invalidating existing sessions.
- [`rotate_client_secret`](../app/native_auth/client_administration.py#L251) — L251

  ```python
  async rotate_client_secret(sessions: async_sessionmaker[AsyncSession], plan: RotateClientSecretPlan, *, actor_ref: str, idempotency_key: str, client_secret_hash: str) -> AdministrationResult
  ```
- [`admit_client`](../app/native_auth/client_administration.py#L295) — L295

  ```python
  async admit_client(sessions: async_sessionmaker[AsyncSession], plan: AdmitClientPlan, *, actor_ref: str, idempotency_key: str) -> AdministrationResult
  ```
  Explicitly admit a client to Stage authentication while preserving MFA strength.

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_operator`](../app/native_auth/client_administration.py#L103) — L103

  ```python
  _operator(actor_ref: str, idempotency_key: str) -> None
  ```
- [`_secret_hash`](../app/native_auth/client_administration.py#L113) — L113

  ```python
  _secret_hash(value: str | None, required: bool) -> None
  ```
- [`_lock_domain`](../app/native_auth/client_administration.py#L138) — L138

  ```python
  async _lock_domain(session: AsyncSession, plan: ClientPlan) -> tuple[TrustStateTable, TenantGuardTable]
  ```
- [`_invalidate_client`](../app/native_auth/client_administration.py#L157) — L157

  ```python
  async _invalidate_client(session: AsyncSession, client: NativeClientTable, guard: TenantGuardTable) -> None
  ```
- [`_event`](../app/native_auth/client_administration.py#L169) — L169

  ```python
  _event(session: AsyncSession, client: NativeClientTable, kind: str) -> None
  ```

</details>

### app/native_auth/config.py

规范化固定 issuer、生命周期与来源预算；不信任 Host/Forwarded 自报发行者。

[打开源码](../app/native_auth/config.py) · 99 行

类：[`NativeAuthConfig`](../app/native_auth/config.py#L10)。

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`NativeAuthConfig.__post_init__`](../app/native_auth/config.py#L26) — L26

  ```python
  NativeAuthConfig.__post_init__(self) -> None
  ```

</details>

### app/native_auth/credentials.py

密码/TOTP/恢复和单次重绑授权，账号失败计数、凭据版本与当前会话撤销。

[打开源码](../app/native_auth/credentials.py) · 761 行

类：[`LoginProgress`](../app/native_auth/credentials.py#L46)；[`FactorResetResult`](../app/native_auth/credentials.py#L56)；[`AdminResetResult`](../app/native_auth/credentials.py#L62)；[`NativeCredentials`](../app/native_auth/credentials.py#L135)。

主要可调用定义：

- [`acquire_account_lock`](../app/native_auth/credentials.py#L67) — L67

  ```python
  async acquire_account_lock(session: AsyncSession, account_ref: str, *, organization_ref: str, trust_ref: str) -> NativeAccountTable
  ```
  Same trust -> principal -> organization order as human business commands.
- [`authentication_event`](../app/native_auth/credentials.py#L106) — L106

  ```python
  authentication_event(session: AsyncSession, event: str, *, account_ref: str | None=None, client_id: str | None=None, version: int | None=None, actor_ref: str | None=None) -> None
  ```
  A deliberately tiny audit whitelist excludes tokens, factors and login data.
- [`commit_authentication`](../app/native_auth/credentials.py#L126) — L126

  ```python
  async commit_authentication(session: AsyncSession) -> None
  ```
- [`NativeCredentials.lock_account`](../app/native_auth/credentials.py#L166) — L166

  ```python
  async NativeCredentials.lock_account(self, session: AsyncSession, account_ref: str) -> NativeAccountTable
  ```
- [`NativeCredentials.validate_browser_request`](../app/native_auth/credentials.py#L175) — L175

  ```python
  async NativeCredentials.validate_browser_request(session: AsyncSession, request_ref: str, browser_secret: str, csrf_token: str | None, *, lock: bool=False) -> NativeLoginRequestTable
  ```
- [`NativeCredentials.throttle_authorization`](../app/native_auth/credentials.py#L238) — L238

  ```python
  async NativeCredentials.throttle_authorization(self, client_address: str) -> None
  ```
  Separate bound for persistent anonymous browser authorization requests.
- [`NativeCredentials.throttle_logout`](../app/native_auth/credentials.py#L242) — L242

  ```python
  async NativeCredentials.throttle_logout(self, client_address: str) -> None
  ```
  Bound anonymous logout requests separately from login ceremonies.
- [`NativeCredentials.progress`](../app/native_auth/credentials.py#L290) — L290

  ```python
  async NativeCredentials.progress(self, request_ref: str, browser_secret: str) -> LoginProgress
  ```
- [`NativeCredentials.password`](../app/native_auth/credentials.py#L304) — L304

  ```python
  async NativeCredentials.password(self, request_ref: str, browser_secret: str, csrf_token: str, username: str, password: str, *, client_address: str='local') -> LoginProgress
  ```
- [`NativeCredentials.otp`](../app/native_auth/credentials.py#L400) — L400

  ```python
  async NativeCredentials.otp(self, request_ref: str, browser_secret: str, csrf_token: str, code: str, *, client_address: str='local') -> LoginProgress
  ```
- [`NativeCredentials.recover`](../app/native_auth/credentials.py#L456) — L456

  ```python
  async NativeCredentials.recover(self, request_ref: str, browser_secret: str, csrf_token: str, recovery_code: str, *, client_address: str='local') -> LoginProgress
  ```
- [`NativeCredentials.create_account`](../app/native_auth/credentials.py#L505) — L505

  ```python
  async NativeCredentials.create_account(self, account_ref: str, principal_ref: str, username: str, password: str) -> str
  ```
  Trusted local administration only; never creates a Principal or membership.
- [`NativeCredentials.change_password`](../app/native_auth/credentials.py#L619) — L619

  ```python
  async NativeCredentials.change_password(self, session_ref: str, old_password: str, new_password: str, *, access_claims: dict[str, Any] | None=None) -> int
  ```
- [`NativeCredentials.reset_totp`](../app/native_auth/credentials.py#L654) — L654

  ```python
  async NativeCredentials.reset_totp(self, session_ref: str, password: str, otp: str, *, access_claims: dict[str, Any] | None=None) -> FactorResetResult
  ```
- [`NativeCredentials.disable_account`](../app/native_auth/credentials.py#L698) — L698

  ```python
  async NativeCredentials.disable_account(self, account_ref: str, expected_version: int, *, actor_ref: str | None=None) -> int
  ```
  Explicit OS operator operation; never a caller-controlled public endpoint.
- [`NativeCredentials.admin_reset`](../app/native_auth/credentials.py#L718) — L718

  ```python
  async NativeCredentials.admin_reset(self, account_ref: str, expected_version: int, *, new_password: str | None=None, reset_totp: bool=False, actor_ref: str | None=None) -> AdminResetResult
  ```
  Trusted explicit OS recovery; no mailbox lookup or automatic identity linking.

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`NativeCredentials.__init__`](../app/native_auth/credentials.py#L136) — L136

  ```python
  NativeCredentials.__init__(self, sessions: async_sessionmaker[AsyncSession], *, fernet_key: bytes | str, organization_ref: str, trust_ref: str, issuer_label: str='OrchestraCorp', peer_attempt_limit: int=30, authorization_attempt_limit: int=30, rate_window_seconds: int=600) -> None
  ```
- [`NativeCredentials._throttle`](../app/native_auth/credentials.py#L201) — L201

  ```python
  async NativeCredentials._throttle(self, client_address: str, *, limit: int | None=None) -> None
  ```
  Bound work before Argon2; source address is transport peer, never forwarded headers.
- [`NativeCredentials._active`](../app/native_auth/credentials.py#L247) — L247

  ```python
  NativeCredentials._active(account: NativeAccountTable, now: datetime) -> bool
  ```
- [`NativeCredentials._failed`](../app/native_auth/credentials.py#L251) — L251

  ```python
  NativeCredentials._failed(session: AsyncSession, account: NativeAccountTable | None, client_id: str, now: datetime) -> None
  ```
- [`NativeCredentials._revoke_sessions`](../app/native_auth/credentials.py#L261) — L261

  ```python
  async NativeCredentials._revoke_sessions(self, session: AsyncSession, account_ref: str, now: datetime) -> None
  ```
- [`NativeCredentials._progress`](../app/native_auth/credentials.py#L276) — L276

  ```python
  NativeCredentials._progress(self, request: NativeLoginRequestTable, account: NativeAccountTable | None=None) -> LoginProgress
  ```
- [`NativeCredentials._factor_context`](../app/native_auth/credentials.py#L363) — L363

  ```python
  async NativeCredentials._factor_context(self, session: AsyncSession, request_ref: str, browser_secret: str, csrf_token: str) -> tuple[NativeLoginRequestTable, NativeAccountTable]
  ```
- [`NativeCredentials._factor_deadline`](../app/native_auth/credentials.py#L390) — L390

  ```python
  NativeCredentials._factor_deadline(request: NativeLoginRequestTable) -> None
  ```
- [`NativeCredentials._session_snapshot`](../app/native_auth/credentials.py#L552) — L552

  ```python
  async NativeCredentials._session_snapshot(self, session_ref: str) -> tuple[str, str, int]
  ```
- [`NativeCredentials._strong_session`](../app/native_auth/credentials.py#L560) — L560

  ```python
  async NativeCredentials._strong_session(self, session: AsyncSession, session_ref: str, account: NativeAccountTable, *, version: int) -> tuple[NativeSessionTable, datetime]
  ```
- [`NativeCredentials._access_proof`](../app/native_auth/credentials.py#L594) — L594

  ```python
  async NativeCredentials._access_proof(self, session: AsyncSession, claims: dict[str, Any] | None, login: NativeSessionTable) -> None
  ```
- [`NativeCredentials._final_deadline`](../app/native_auth/credentials.py#L610) — L610

  ```python
  NativeCredentials._final_deadline(login: NativeSessionTable, claims: dict[str, Any] | None, authentication_deadline: datetime) -> None
  ```

</details>

### app/native_auth/crypto.py

Argon2id 有界计算、加密 TOTP、随机秘密/摘要与恢复码。

[打开源码](../app/native_auth/crypto.py) · 118 行

类：[`TotpCipher`](../app/native_auth/crypto.py#L87)。

主要可调用定义：

- [`digest_secret`](../app/native_auth/crypto.py#L26) — L26

  ```python
  digest_secret(value: str) -> str
  ```
- [`random_secret`](../app/native_auth/crypto.py#L30) — L30

  ```python
  random_secret() -> str
  ```
- [`normalize_username`](../app/native_auth/crypto.py#L34) — L34

  ```python
  normalize_username(value: str) -> str
  ```
- [`validate_password`](../app/native_auth/crypto.py#L41) — L41

  ```python
  validate_password(password: str) -> None
  ```
- [`hash_password`](../app/native_auth/crypto.py#L67) — L67

  ```python
  async hash_password(password: str) -> str
  ```
- [`verify_password`](../app/native_auth/crypto.py#L81) — L81

  ```python
  async verify_password(stored: str | None, password: str) -> bool
  ```
- [`TotpCipher.new_secret`](../app/native_auth/crypto.py#L91) — L91

  ```python
  TotpCipher.new_secret(self) -> str
  ```
- [`TotpCipher.encrypt`](../app/native_auth/crypto.py#L94) — L94

  ```python
  TotpCipher.encrypt(self, secret: str) -> str
  ```
- [`TotpCipher.decrypt`](../app/native_auth/crypto.py#L97) — L97

  ```python
  TotpCipher.decrypt(self, encrypted: str) -> str
  ```
- [`TotpCipher.uri`](../app/native_auth/crypto.py#L103) — L103

  ```python
  TotpCipher.uri(self, secret: str, username: str, issuer: str) -> str
  ```
- [`TotpCipher.verified_step`](../app/native_auth/crypto.py#L106) — L106

  ```python
  TotpCipher.verified_step(self, secret: str, code: str, *, now: datetime, last_step: int) -> int | None
  ```
- [`recovery_codes`](../app/native_auth/crypto.py#L117) — L117

  ```python
  recovery_codes() -> tuple[str, ...]
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_password_work`](../app/native_auth/crypto.py#L46) — L46

  ```python
  async _password_work(operation: Callable[..., T], *args: Any) -> T
  ```
- [`_verify`](../app/native_auth/crypto.py#L74) — L74

  ```python
  _verify(stored: str, password: str) -> bool
  ```
- [`TotpCipher.__init__`](../app/native_auth/crypto.py#L88) — L88

  ```python
  TotpCipher.__init__(self, key: bytes | str) -> None
  ```

</details>

### app/native_auth/current_session.py

独立的当前账号、客户端和会话事实重验，调用方保有锁序。

[打开源码](../app/native_auth/current_session.py) · 120 行

主要可调用定义：

- [`validate_browser_session`](../app/native_auth/current_session.py#L22) — L22

  ```python
  async validate_browser_session(session: AsyncSession, browser_hash: str, account: NativeAccountTable, auth_time: datetime, *, now: datetime) -> NativeBrowserSessionTable
  ```
- [`validate_native_session`](../app/native_auth/current_session.py#L39) — L39

  ```python
  async validate_native_session(session: AsyncSession, claims: dict[str, Any], *, now: datetime) -> tuple[NativeAccountTable, NativeSessionTable]
  ```
  Refresh current native facts after the caller acquired authority locks.

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_integer`](../app/native_auth/current_session.py#L18) — L18

  ```python
  _integer(value: Any) -> bool
  ```

</details>

### app/native_auth/errors.py

脱敏凭据错误和提交未知。

[打开源码](../app/native_auth/errors.py) · 11 行

类：[`NativeCredentialError`](../app/native_auth/errors.py#L4)；[`NativeCommitUnknown`](../app/native_auth/errors.py#L10)。

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`NativeCredentialError.__init__`](../app/native_auth/errors.py#L5) — L5

  ```python
  NativeCredentialError.__init__(self, code: str='invalid_credentials') -> None
  ```

</details>

### app/native_auth/freshness.py

锁内按当前组织策略和操作上限计算认证截止时间。

[打开源码](../app/native_auth/freshness.py) · 35 行

主要可调用定义：

- [`current_authentication_deadline`](../app/native_auth/freshness.py#L10) — L10

  ```python
  async current_authentication_deadline(session: AsyncSession, organization_ref: str, authenticated_at: datetime, *, default_seconds: int, max_age_seconds: int | None=None) -> datetime
  ```
  Combine the operation, RP and current organization limits without refreshing MFA.

### app/native_auth/keys.py

私有 RSA/Fernet 文件检查、签名/public JWKS、access 与 logout hint 验签。

[打开源码](../app/native_auth/keys.py) · 208 行

类：[`NativeKeyError`](../app/native_auth/keys.py#L15)；[`KeyRing`](../app/native_auth/keys.py#L44)。

主要可调用定义：

- [`KeyRing.sign`](../app/native_auth/keys.py#L88) — L88

  ```python
  KeyRing.sign(self, claims: dict[str, Any], typ: str='at+jwt') -> str
  ```
- [`KeyRing.verify_access`](../app/native_auth/keys.py#L93) — L93

  ```python
  KeyRing.verify_access(self, token: str, issuer: str, audience: str | list[str]) -> dict[str, Any]
  ```
- [`KeyRing.verify_id_token_hint`](../app/native_auth/keys.py#L132) — L132

  ```python
  KeyRing.verify_id_token_hint(self, token: str, issuer: str, client_id: str | None=None) -> dict[str, Any]
  ```
  Validate an RP logout hint, including an expired token, never authentication.
- [`KeyRing.jwks`](../app/native_auth/keys.py#L183) — L183

  ```python
  KeyRing.jwks(self) -> dict[str, Any]
  ```
- [`load_keyring`](../app/native_auth/keys.py#L192) — L192

  ```python
  load_keyring(path: str | Path, environment: str='dev') -> KeyRing
  ```
- [`read_encryption_key`](../app/native_auth/keys.py#L202) — L202

  ```python
  read_encryption_key(path: str | Path, environment: str='dev') -> bytes
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_unique_object`](../app/native_auth/keys.py#L19) — L19

  ```python
  _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]
  ```
- [`_private_bytes`](../app/native_auth/keys.py#L28) — L28

  ```python
  _private_bytes(path: str | Path) -> bytes
  ```
- [`KeyRing.__init__`](../app/native_auth/keys.py#L45) — L45

  ```python
  KeyRing.__init__(self, active_kid: str, keys: list[dict[str, str]]) -> None
  ```

</details>

### app/native_auth/logout.py

RP 退出请求/确认、Cookie/CSRF 绑定、当前浏览器相关 RP 会话撤销。

[打开源码](../app/native_auth/logout.py) · 268 行

类：[`LogoutChallenge`](../app/native_auth/logout.py#L34)；[`LogoutResult`](../app/native_auth/logout.py#L43)；[`NativeLogout`](../app/native_auth/logout.py#L57)。

主要可调用定义：

- [`NativeLogout.prepare`](../app/native_auth/logout.py#L92) — L92

  ```python
  async NativeLogout.prepare(self, *, id_token_hint: str | None=None, client_id: str | None=None, post_logout_redirect_uri: str | None=None, state: str | None=None, sso_cookie: str | None=None) -> LogoutChallenge
  ```
- [`NativeLogout.confirm`](../app/native_auth/logout.py#L189) — L189

  ```python
  async NativeLogout.confirm(self, *, request_ref: str, browser_secret: str, csrf_token: str, sso_cookie: str | None=None, confirm: bool) -> LogoutResult
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_text`](../app/native_auth/logout.py#L49) — L49

  ```python
  _text(value: Any, limit: int, *, empty: bool=False) -> bool
  ```
- [`NativeLogout.__init__`](../app/native_auth/logout.py#L58) — L58

  ```python
  NativeLogout.__init__(self, sessions: async_sessionmaker[AsyncSession], config: NativeAuthConfig, *, credentials: NativeCredentials, oauth: NativeOAuth) -> None
  ```
- [`NativeLogout._client`](../app/native_auth/logout.py#L71) — L71

  ```python
  async NativeLogout._client(self, session: AsyncSession, client_id: str | None, redirect_uri: str | None) -> NativeClientTable | None
  ```
- [`NativeLogout._lock_browser`](../app/native_auth/logout.py#L152) — L152

  ```python
  async NativeLogout._lock_browser(self, session: AsyncSession, session_hash: str) -> NativeBrowserSessionTable
  ```
- [`NativeLogout._proof`](../app/native_auth/logout.py#L179) — L179

  ```python
  NativeLogout._proof(request: NativeLogoutRequestTable | None, browser_secret: str, csrf_token: str, now: datetime) -> None
  ```

</details>

### app/native_auth/oauth.py

PKCE、client auth、token/refresh/revoke/UserInfo；当前会话校验复用独立模块。

[打开源码](../app/native_auth/oauth.py) · 708 行

类：[`NativeOAuth`](../app/native_auth/oauth.py#L75)。

主要可调用定义：

- [`consent_satisfied`](../app/native_auth/oauth.py#L60) — L60

  ```python
  async consent_satisfied(session: AsyncSession, request: NativeLoginRequestTable, client: NativeClientTable) -> bool
  ```
- [`NativeOAuth.prepare_authorization`](../app/native_auth/oauth.py#L89) — L89

  ```python
  async NativeOAuth.prepare_authorization(self, *, client_id: str, redirect_uri: str, response_type: str, scope: str, code_challenge: str, code_challenge_method: str, state: str | None=None, nonce: str | None=None, response_mode: str='query', prompt: str='', max_age: int | str | None=None, acr_values: str | None=None, login_hint: str | None=None) -> AuthorizationRequest
  ```
- [`NativeOAuth.finish_authorization_response`](../app/native_auth/oauth.py#L195) — L195

  ```python
  async NativeOAuth.finish_authorization_response(self, *, request_ref: str, browser_secret: str, csrf_token: str) -> AuthorizationResponse
  ```
- [`NativeOAuth.finish_authorization`](../app/native_auth/oauth.py#L312) — L312

  ```python
  async NativeOAuth.finish_authorization(self, *, request_ref: str, browser_secret: str, csrf_token: str) -> str
  ```
- [`NativeOAuth.complete_authorization`](../app/native_auth/oauth.py#L318) — L318

  ```python
  async NativeOAuth.complete_authorization(self, *, request_ref: str, browser_secret: str, csrf_token: str) -> str
  ```
- [`NativeOAuth.exchange_code`](../app/native_auth/oauth.py#L323) — L323

  ```python
  async NativeOAuth.exchange_code(self, *, client_id: str, redirect_uri: str, code: str, code_verifier: str, client_secret: str | None=None, client_auth_method: str='none') -> dict[str, Any]
  ```
- [`NativeOAuth.exchange_refresh`](../app/native_auth/oauth.py#L386) — L386

  ```python
  async NativeOAuth.exchange_refresh(self, *, client_id: str, refresh_token: str, client_secret: str | None=None, client_auth_method: str='none', scope: str | None=None) -> dict[str, Any]
  ```
- [`NativeOAuth.revoke`](../app/native_auth/oauth.py#L443) — L443

  ```python
  async NativeOAuth.revoke(self, *, client_id: str, token: str, token_type_hint: str | None=None, client_secret: str | None=None, client_auth_method: str='none') -> None
  ```
- [`NativeOAuth.authenticate_account`](../app/native_auth/oauth.py#L483) — L483

  ```python
  async NativeOAuth.authenticate_account(self, token: str, *, max_age_seconds: int | None=300, allow_userinfo_audience: bool=False) -> dict[str, Any]
  ```
- [`NativeOAuth.userinfo`](../app/native_auth/oauth.py#L520) — L520

  ```python
  async NativeOAuth.userinfo(self, token: str) -> dict[str, str]
  ```
- [`NativeOAuth.authenticate_client`](../app/native_auth/oauth.py#L530) — L530

  ```python
  async NativeOAuth.authenticate_client(self, session: AsyncSession, client_id: str, client_secret: str | None=None, client_auth_method: str='none') -> NativeClientTable
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`NativeOAuth.__init__`](../app/native_auth/oauth.py#L76) — L76

  ```python
  NativeOAuth.__init__(self, sessions: async_sessionmaker[AsyncSession], config: NativeAuthConfig, keyring: KeyRing, *, credentials: NativeCredentials) -> None
  ```
- [`NativeOAuth.prepare_authorization.reject`](../app/native_auth/oauth.py#L127) — L127

  ```python
  NativeOAuth.prepare_authorization.reject(error: str) -> NoReturn
  ```
- [`NativeOAuth._authenticated_account`](../app/native_auth/oauth.py#L492) — L492

  ```python
  async NativeOAuth._authenticated_account(self, token: str, *, max_age_seconds: int | None, allow_userinfo_audience: bool) -> AsyncIterator[tuple[NativeAccountTable, dict[str, Any]]]
  ```
  Keep current account reads in the same authority transaction as verification.
- [`NativeOAuth._lock_account`](../app/native_auth/oauth.py#L551) — L551

  ```python
  async NativeOAuth._lock_account(self, session: AsyncSession, account_ref: str, error: str='invalid_grant', *, status_code: int=400) -> NativeAccountTable
  ```
- [`NativeOAuth._lock_session`](../app/native_auth/oauth.py#L559) — L559

  ```python
  async NativeOAuth._lock_session(self, session: AsyncSession, session_ref: str, *, allow_inactive: bool=False) -> NativeSessionTable
  ```
- [`NativeOAuth._client`](../app/native_auth/oauth.py#L594) — L594

  ```python
  async NativeOAuth._client(self, session: AsyncSession, client_id: str, redirect_uri: str | None=None) -> NativeClientTable
  ```
- [`NativeOAuth._live_session`](../app/native_auth/oauth.py#L610) — L610

  ```python
  NativeOAuth._live_session(login: NativeSessionTable, now: datetime) -> None
  ```
- [`NativeOAuth._issue_tokens`](../app/native_auth/oauth.py#L620) — L620

  ```python
  NativeOAuth._issue_tokens(self, session: AsyncSession, login: NativeSessionTable, now: datetime, client: NativeClientTable, *, include_nonce: bool=True, scope: str | None=None) -> dict[str, Any]
  ```
- [`NativeOAuth._event`](../app/native_auth/oauth.py#L691) — L691

  ```python
  NativeOAuth._event(session: AsyncSession, event: str, login: NativeSessionTable) -> None
  ```
- [`NativeOAuth._commit`](../app/native_auth/oauth.py#L702) — L702

  ```python
  async NativeOAuth._commit(session: AsyncSession) -> None
  ```

</details>

### app/native_auth/protocol.py

OAuth 值对象、范围规范化、响应及错误合同，无数据库依赖。

[打开源码](../app/native_auth/protocol.py) · 61 行

类：[`AuthorizationResponse`](../app/native_auth/protocol.py#L13)；[`OAuthError`](../app/native_auth/protocol.py#L26)；[`AuthorizationRequest`](../app/native_auth/protocol.py#L37)。

主要可调用定义：

- [`AuthorizationResponse.location`](../app/native_auth/protocol.py#L20) — L20

  ```python
  AuthorizationResponse.location(self) -> str
  ```
- [`valid_text`](../app/native_auth/protocol.py#L44) — L44

  ```python
  valid_text(value: Any, limit: int, *, minimum: int=1) -> bool
  ```
- [`normalize_scope`](../app/native_auth/protocol.py#L55) — L55

  ```python
  normalize_scope(value: Any) -> str
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`OAuthError.__init__`](../app/native_auth/protocol.py#L29) — L29

  ```python
  OAuthError.__init__(self, error: str, status_code: int=400, *, response: AuthorizationResponse | None=None) -> None
  ```

</details>

### app/native_auth/redirects.py

精确回调与 issuer/RP 主机隔离；仅 dev/test loopback 例外。

[打开源码](../app/native_auth/redirects.py) · 54 行

主要可调用定义：

- [`validate_redirect_uri`](../app/native_auth/redirects.py#L11) — L11

  ```python
  validate_redirect_uri(uri: str) -> None
  ```
  Only exact, unambiguous HTTPS or explicit loopback callbacks are registered.
- [`validate_issuer_redirect`](../app/native_auth/redirects.py#L35) — L35

  ```python
  validate_issuer_redirect(issuer: str, uri: str, environment: str) -> None
  ```
  Keep host-only authentication cookies away from registered applications.

### app/native_auth/sessions.py

当前人员的会话/应用授权列表、单个撤销、全退出和同意撤销。

[打开源码](../app/native_auth/sessions.py) · 322 行

类：[`AccountSessions`](../app/native_auth/sessions.py#L46)。

主要可调用定义：

- [`AccountSessions.list_sessions`](../app/native_auth/sessions.py#L112) — L112

  ```python
  async AccountSessions.list_sessions(self, access_token: str, *, limit: int=20, after: str | None=None) -> dict[str, Any]
  ```
- [`AccountSessions.revoke_session`](../app/native_auth/sessions.py#L169) — L169

  ```python
  async AccountSessions.revoke_session(self, access_token: str, session_ref: str) -> None
  ```
- [`AccountSessions.revoke_all`](../app/native_auth/sessions.py#L200) — L200

  ```python
  async AccountSessions.revoke_all(self, access_token: str) -> dict[str, int]
  ```
- [`AccountSessions.list_consents`](../app/native_auth/sessions.py#L230) — L230

  ```python
  async AccountSessions.list_consents(self, access_token: str, *, limit: int=20, after: str | None=None) -> dict[str, Any]
  ```
- [`AccountSessions.revoke_consent`](../app/native_auth/sessions.py#L263) — L263

  ```python
  async AccountSessions.revoke_consent(self, access_token: str, client_id: str) -> None
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_reference`](../app/native_auth/sessions.py#L33) — L33

  ```python
  _reference(value: Any) -> bool
  ```
- [`_page`](../app/native_auth/sessions.py#L41) — L41

  ```python
  _page(limit: int, after: str | None) -> None
  ```
- [`AccountSessions.__init__`](../app/native_auth/sessions.py#L47) — L47

  ```python
  AccountSessions.__init__(self, sessions: async_sessionmaker[AsyncSession], config: NativeAuthConfig, *, credentials: NativeCredentials, oauth: NativeOAuth) -> None
  ```
- [`AccountSessions._actor`](../app/native_auth/sessions.py#L61) — L61

  ```python
  async AccountSessions._actor(self, access_token: str, *, fresh: bool) -> AsyncIterator[tuple[AsyncSession, NativeAccountTable, NativeSessionTable, dict[str, Any], datetime | None]]
  ```
- [`AccountSessions._deadline`](../app/native_auth/sessions.py#L87) — L87

  ```python
  AccountSessions._deadline(claims: dict[str, Any], login: NativeSessionTable, *, authentication_deadline: datetime | None) -> None
  ```
- [`AccountSessions._event`](../app/native_auth/sessions.py#L97) — L97

  ```python
  AccountSessions._event(session: AsyncSession, event: str, account: NativeAccountTable, *, client_id: str | None=None, target_session: str | None=None) -> None
  ```

</details>

### app/providers/__init__.py

包初始化与公开符号导出。

[打开源码](../app/providers/__init__.py) · 1 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/providers/assignments.py

本人任职应用服务：生命周期租约、真实身份、当前授权读取与截止复核。

[打开源码](../app/providers/assignments.py) · 58 行

类：[`AssignmentService`](../app/providers/assignments.py#L17)。

主要可调用定义：

- [`AssignmentService.list_self`](../app/providers/assignments.py#L22) — L22

  ```python
  async AssignmentService.list_self(self, token: str, *, profile_ref: str, profile_version: int, limit: int=20, after: str | None=None) -> AssignmentList
  ```
- [`assignments_plugin`](../app/providers/assignments.py#L54) — L54

  ```python
  assignments_plugin(config: Settings) -> PluginSpec
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`AssignmentService.__init__`](../app/providers/assignments.py#L18) — L18

  ```python
  AssignmentService.__init__(self, context: ComponentContext, timeout_seconds: float) -> None
  ```
- [`assignments_plugin.setup`](../app/providers/assignments.py#L55) — L55

  ```python
  assignments_plugin.setup(ctx: ComponentContext) -> None
  ```

</details>

### app/providers/execution.py

内部通用命令/投递装配工厂；默认未挂载，不能直接当公开人员命令模板。

[打开源码](../app/providers/execution.py) · 104 行

类：[`CommandService`](../app/providers/execution.py#L24)。

主要可调用定义：

- [`CommandService.execute`](../app/providers/execution.py#L41) — L41

  ```python
  async CommandService.execute(self, context: ExecutionContext, command: CommandEnvelope) -> CommandResult
  ```
- [`CommandService.get_result`](../app/providers/execution.py#L52) — L52

  ```python
  async CommandService.get_result(self, context: ExecutionContext, execution_id: str) -> CommandResult
  ```
- [`command_plugin`](../app/providers/execution.py#L64) — L64

  ```python
  command_plugin(loader: AuthorizationLoader, commands: Sequence[CommandSpec], *, coordinator: AuthorizationLockCoordinator | None=None, identity_validator: CommandIdentityValidator | None=None) -> PluginSpec
  ```
- [`delivery_plugin`](../app/providers/execution.py#L77) — L77

  ```python
  delivery_plugin(*, authorizer: CurrentDeliveryAuthorizer, sinks: Mapping[str, DeliverySink], service_ref: str, worker_ref: str, tenants: Sequence[str], close_timeout: float=5, poll_interval: float=1) -> PluginSpec
  ```
  Mount only after a trusted authorizer and explicit sink/tenant scope exist.

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`CommandService.__init__`](../app/providers/execution.py#L27) — L27

  ```python
  CommandService.__init__(self, context: ComponentContext, loader: AuthorizationLoader, commands: Sequence[CommandSpec], coordinator: AuthorizationLockCoordinator | None=None, identity_validator: CommandIdentityValidator | None=None) -> None
  ```
- [`command_plugin.setup`](../app/providers/execution.py#L71) — L71

  ```python
  command_plugin.setup(ctx: ComponentContext) -> None
  ```
- [`delivery_plugin.setup`](../app/providers/execution.py#L89) — L89

  ```python
  delivery_plugin.setup(ctx: ComponentContext) -> None
  ```

</details>

### app/providers/human.py

公共 HumanOperations.request 与完整人员 kernel 装配；统一身份、租约和截止。

[打开源码](../app/providers/human.py) · 88 行

类：[`HumanExecution`](../app/providers/human.py#L52)；[`HumanOperations`](../app/providers/human.py#L57)。

主要可调用定义：

- [`human_execution_context`](../app/providers/human.py#L21) — L21

  ```python
  human_execution_context(evidence: HumanAuthenticationEvidence, *, assignment_ref: str, request_id: str, deadline: datetime) -> ExecutionContext
  ```
- [`create_human_kernel`](../app/providers/human.py#L41) — L41

  ```python
  create_human_kernel(sessions: async_sessionmaker[AsyncSession], identity: HumanIdentityVerifier, commands: Sequence[CommandSpec]) -> CommandKernel
  ```
  Install all three identity hooks together; none may be omitted.
- [`HumanOperations.request`](../app/providers/human.py#L70) — L70

  ```python
  async HumanOperations.request(self, token: str, *, profile_ref: str, profile_version: int, assignment_ref: str, request_id: str) -> AsyncIterator[HumanExecution]
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`HumanOperations.__init__`](../app/providers/human.py#L64) — L64

  ```python
  HumanOperations.__init__(self, context: ComponentContext, timeout_seconds: float, commands: Sequence[CommandSpec]) -> None
  ```

</details>

### app/providers/identity.py

IdentityFacade、固定存储依赖和验证器关闭。

[打开源码](../app/providers/identity.py) · 70 行

类：[`IdentityFacade`](../app/providers/identity.py#L17)。

主要可调用定义：

- [`IdentityFacade.read_self`](../app/providers/identity.py#L22) — L22

  ```python
  async IdentityFacade.read_self(self, access_token: str, *, profile_ref: str, profile_version: int) -> SelfIdentity
  ```
- [`IdentityFacade.authenticate_human`](../app/providers/identity.py#L36) — L36

  ```python
  async IdentityFacade.authenticate_human(self, access_token: str, *, profile_ref: str, profile_version: int) -> HumanAuthenticationEvidence
  ```
- [`IdentityFacade.acquire_human_locks`](../app/providers/identity.py#L48) — L48

  ```python
  async IdentityFacade.acquire_human_locks(self, session: AsyncSession, evidence: HumanAuthenticationEvidence) -> None
  ```
- [`IdentityFacade.validate_human`](../app/providers/identity.py#L52) — L52

  ```python
  async IdentityFacade.validate_human(self, session: AsyncSession, evidence: HumanAuthenticationEvidence, now: datetime) -> None
  ```
- [`identity_plugin`](../app/providers/identity.py#L56) — L56

  ```python
  identity_plugin(config: Settings) -> PluginSpec
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`IdentityFacade.__init__`](../app/providers/identity.py#L18) — L18

  ```python
  IdentityFacade.__init__(self, context: ComponentContext, service: IdentityService) -> None
  ```
- [`identity_plugin.setup`](../app/providers/identity.py#L57) — L57

  ```python
  identity_plugin.setup(ctx: ComponentContext) -> None
  ```

</details>

### app/providers/native_auth.py

固定部署绑定检查、原生各服务工厂、操作期限和 STORAGE lease。

[打开源码](../app/providers/native_auth.py) · 119 行

类：[`NativeAuthServices`](../app/providers/native_auth.py#L32)；[`NativeAuthFacade`](../app/providers/native_auth.py#L40)。

主要可调用定义：

- [`NativeAuthFacade.services`](../app/providers/native_auth.py#L50) — L50

  ```python
  async NativeAuthFacade.services(self) -> AsyncIterator[NativeAuthServices]
  ```
- [`native_auth_plugin`](../app/providers/native_auth.py#L101) — L101

  ```python
  native_auth_plugin(settings: Settings) -> PluginSpec
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`NativeAuthFacade.__init__`](../app/providers/native_auth.py#L41) — L41

  ```python
  NativeAuthFacade.__init__(self, context: ComponentContext, config: NativeAuthConfig, keyring: KeyRing, encryption_key: bytes) -> None
  ```
- [`_validate_deployment`](../app/providers/native_auth.py#L70) — L70

  ```python
  async _validate_deployment(sessions: async_sessionmaker[AsyncSession], config: NativeAuthConfig) -> None
  ```
  Bind the issuer to its fixed deployment, independently of business admission.
- [`native_auth_plugin.setup`](../app/providers/native_auth.py#L102) — L102

  ```python
  async native_auth_plugin.setup(ctx: ComponentContext) -> None
  ```

</details>

### app/providers/storage.py

数据库 schema/head、PG/Redis 连接池及周期探测、健康状态通知。

[打开源码](../app/providers/storage.py) · 134 行

类：[`Storage`](../app/providers/storage.py#L21)；[`DependencyHealth`](../app/providers/storage.py#L27)。

主要可调用定义：

- [`schema_heads`](../app/providers/storage.py#L36) — L36

  ```python
  schema_heads() -> frozenset[str]
  ```
- [`probe_database`](../app/providers/storage.py#L40) — L40

  ```python
  async probe_database(engine: AsyncEngine, expected: frozenset[str], timeout: float) -> None
  ```
- [`database_plugin`](../app/providers/storage.py#L48) — L48

  ```python
  database_plugin(config: Settings, health: DependencyHealth) -> PluginSpec
  ```
- [`redis_plugin`](../app/providers/storage.py#L92) — L92

  ```python
  redis_plugin(config: Settings, health: DependencyHealth) -> PluginSpec
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`database_plugin.setup`](../app/providers/storage.py#L49) — L49

  ```python
  async database_plugin.setup(ctx: ComponentContext) -> None
  ```
- [`database_plugin.setup.monitor`](../app/providers/storage.py#L69) — L69

  ```python
  async database_plugin.setup.monitor() -> None
  ```
- [`redis_plugin.setup`](../app/providers/storage.py#L93) — L93

  ```python
  async redis_plugin.setup(ctx: ComponentContext) -> None
  ```
- [`redis_plugin.setup.probe`](../app/providers/storage.py#L97) — L97

  ```python
  async redis_plugin.setup.probe() -> None
  ```
- [`redis_plugin.setup.monitor`](../app/providers/storage.py#L114) — L114

  ```python
  async redis_plugin.setup.monitor() -> None
  ```

</details>

### app/providers/work.py

实际人员工作项编排入口；组合 evidence、单任职 loader、kernel/UoW。

[打开源码](../app/providers/work.py) · 134 行

类：[`WorkItemView`](../app/providers/work.py#L19)；[`HumanWorkService`](../app/providers/work.py#L30)。

主要可调用定义：

- [`HumanWorkService.update_details`](../app/providers/work.py#L34) — L34

  ```python
  async HumanWorkService.update_details(self, token: str, *, work_item_id: int, profile_ref: str, profile_version: int, assignment_ref: str, request_id: str, idempotency_key: str, expected_version: int, details: WorkDetailsInput) -> CommandResult
  ```
- [`HumanWorkService.get_operation`](../app/providers/work.py#L66) — L66

  ```python
  async HumanWorkService.get_operation(self, token: str, *, work_item_id: int, profile_ref: str, profile_version: int, assignment_ref: str, request_id: str, idempotency_key: str) -> CommandResult
  ```
- [`HumanWorkService.get_item`](../app/providers/work.py#L92) — L92

  ```python
  async HumanWorkService.get_item(self, token: str, *, work_item_id: int, profile_ref: str, profile_version: int, assignment_ref: str, request_id: str) -> WorkItemView
  ```
- [`human_work_plugin`](../app/providers/work.py#L130) — L130

  ```python
  human_work_plugin(config: Settings) -> PluginSpec
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`HumanWorkService.__init__`](../app/providers/work.py#L31) — L31

  ```python
  HumanWorkService.__init__(self, context: ComponentContext, timeout_seconds: float) -> None
  ```
- [`HumanWorkService.get_item.read_item`](../app/providers/work.py#L102) — L102

  ```python
  async HumanWorkService.get_item.read_item(session: AsyncSession) -> WorkItemView
  ```
- [`human_work_plugin.setup`](../app/providers/work.py#L131) — L131

  ```python
  human_work_plugin.setup(ctx: ComponentContext) -> None
  ```

</details>

### app/runtime/__init__.py

包初始化与公开符号导出。

[打开源码](../app/runtime/__init__.py) · 35 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/runtime/context.py

ComponentContext 的稳定导出入口；实际实现位于 host.py。

[打开源码](../app/runtime/context.py) · 5 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/runtime/contracts.py

组件/服务键/绑定身份、状态、就绪和关闭报告；无业务身份。

[打开源码](../app/runtime/contracts.py) · 108 行

类：[`RuntimeClosed`](../app/runtime/contracts.py#L9)；[`MissingService`](../app/runtime/contracts.py#L13)；[`DuplicateProvider`](../app/runtime/contracts.py#L17)；[`LifecycleWaitCycle`](../app/runtime/contracts.py#L21)；[`ComponentState`](../app/runtime/contracts.py#L25)；[`ServiceKey`](../app/runtime/contracts.py#L35)；[`BindingIdentity`](../app/runtime/contracts.py#L49)；[`PluginSpec`](../app/runtime/contracts.py#L58)；[`RuntimeFailure`](../app/runtime/contracts.py#L79)；[`ReadyReport`](../app/runtime/contracts.py#L88)；[`CloseReport`](../app/runtime/contracts.py#L100)。

主要可调用定义：

- [`ReadyReport.ready`](../app/runtime/contracts.py#L95) — L95

  ```python
  ReadyReport.ready(self) -> bool
  ```
- [`CloseReport.successful`](../app/runtime/contracts.py#L107) — L107

  ```python
  CloseReport.successful(self) -> bool
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`ServiceKey.__post_init__`](../app/runtime/contracts.py#L40) — L40

  ```python
  ServiceKey.__post_init__(self) -> None
  ```
- [`ServiceKey.__str__`](../app/runtime/contracts.py#L44) — L44

  ```python
  ServiceKey.__str__(self) -> str
  ```
- [`PluginSpec.__post_init__`](../app/runtime/contracts.py#L67) — L67

  ```python
  PluginSpec.__post_init__(self) -> None
  ```

</details>

### app/runtime/effects.py

资源 effect 登记、准备与依赖有序清理，失败可观察。

[打开源码](../app/runtime/effects.py) · 282 行

类：[`EffectClosed`](../app/runtime/effects.py#L20)；[`EffectWaitCycle`](../app/runtime/effects.py#L24)；[`EffectRecord`](../app/runtime/effects.py#L32)；[`EffectReport`](../app/runtime/effects.py#L47)；[`EffectHandle`](../app/runtime/effects.py#L68)；[`EffectScope`](../app/runtime/effects.py#L206)。

主要可调用定义：

- [`EffectRecord.cleanup_complete`](../app/runtime/effects.py#L42) — L42

  ```python
  EffectRecord.cleanup_complete(self) -> bool
  ```
- [`EffectReport.errors`](../app/runtime/effects.py#L51) — L51

  ```python
  EffectReport.errors(self) -> tuple[BaseException, ...]
  ```
- [`EffectReport.successful`](../app/runtime/effects.py#L55) — L55

  ```python
  EffectReport.successful(self) -> bool
  ```
- [`EffectReport.cleanup_complete`](../app/runtime/effects.py#L59) — L59

  ```python
  EffectReport.cleanup_complete(self) -> bool
  ```
  Setup may have failed even though every acquired resource was released.
- [`EffectReport.cleanup_errors`](../app/runtime/effects.py#L64) — L64

  ```python
  EffectReport.cleanup_errors(self) -> tuple[BaseException, ...]
  ```
- [`EffectHandle.snapshot`](../app/runtime/effects.py#L86) — L86

  ```python
  EffectHandle.snapshot(self) -> EffectRecord
  ```
- [`EffectHandle.ready`](../app/runtime/effects.py#L97) — L97

  ```python
  async EffectHandle.ready(self) -> EffectHandle
  ```
- [`EffectHandle.close`](../app/runtime/effects.py#L106) — L106

  ```python
  async EffectHandle.close(self) -> EffectReport
  ```
- [`EffectScope.effect`](../app/runtime/effects.py#L221) — L221

  ```python
  EffectScope.effect(self, setup: Callable[[], Any], label: str='effect', *, depends_on: tuple[EffectHandle, ...]=()) -> EffectHandle
  ```
- [`EffectScope.ready`](../app/runtime/effects.py#L240) — L240

  ```python
  async EffectScope.ready(self) -> None
  ```
- [`EffectScope.stop_admission`](../app/runtime/effects.py#L247) — L247

  ```python
  EffectScope.stop_admission(self) -> None
  ```
- [`EffectScope.snapshot`](../app/runtime/effects.py#L250) — L250

  ```python
  EffectScope.snapshot(self) -> tuple[EffectRecord, ...]
  ```
- [`EffectScope.errors`](../app/runtime/effects.py#L254) — L254

  ```python
  EffectScope.errors(self) -> tuple[BaseException, ...]
  ```
- [`EffectScope.close`](../app/runtime/effects.py#L257) — L257

  ```python
  async EffectScope.close(self) -> EffectReport
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`EffectHandle.__init__`](../app/runtime/effects.py#L69) — L69

  ```python
  EffectHandle.__init__(self, scope: EffectScope, label: str, depends_on: tuple[EffectHandle, ...]) -> None
  ```
- [`EffectHandle._close_shared`](../app/runtime/effects.py#L113) — L113

  ```python
  async EffectHandle._close_shared(self) -> EffectReport
  ```
- [`EffectHandle._close`](../app/runtime/effects.py#L118) — L118

  ```python
  async EffectHandle._close(self) -> EffectReport
  ```
- [`EffectHandle._start`](../app/runtime/effects.py#L139) — L139

  ```python
  EffectHandle._start(self, setup: Callable[[], Any]) -> None
  ```
- [`EffectHandle._consume_sync`](../app/runtime/effects.py#L150) — L150

  ```python
  EffectHandle._consume_sync(self, result: Any) -> None
  ```
- [`EffectHandle._consume_generator`](../app/runtime/effects.py#L166) — L166

  ```python
  EffectHandle._consume_generator(self, generator: Generator[Any, Any, Any]) -> None
  ```
- [`EffectHandle._consume_async`](../app/runtime/effects.py#L177) — L177

  ```python
  async EffectHandle._consume_async(self, result: Any) -> None
  ```
- [`EffectScope.__init__`](../app/runtime/effects.py#L207) — L207

  ```python
  EffectScope.__init__(self, owner_id: str, generation: int=0, *, is_open: Callable[[], bool] | None=None) -> None
  ```
- [`EffectScope._close`](../app/runtime/effects.py#L265) — L265

  ```python
  async EffectScope._close(self) -> EffectReport
  ```
- [`EffectScope._close.block_dependencies`](../app/runtime/effects.py#L268) — L268

  ```python
  EffectScope._close.block_dependencies(handle: EffectHandle) -> None
  ```

</details>

### app/runtime/events.py

进程内 emit/parallel/serial/bail/waterfall；不是持久事件总线。

[打开源码](../app/runtime/events.py) · 333 行

类：[`EventKey`](../app/runtime/events.py#L20)；[`Continue`](../app/runtime/events.py#L34)；[`Stop`](../app/runtime/events.py#L39)；[`EventPin`](../app/runtime/events.py#L43)；[`EventOwner`](../app/runtime/events.py#L47)；[`ListenerHandle`](../app/runtime/events.py#L67)；[`_Snapshot`](../app/runtime/events.py#L112)；[`EventBus`](../app/runtime/events.py#L126)。

主要可调用定义：

- [`EventPin.release`](../app/runtime/events.py#L44) — L44

  ```python
  EventPin.release(self) -> None
  ```
- [`EventOwner.owner_id`](../app/runtime/events.py#L49) — L49

  ```python
  EventOwner.owner_id(self) -> str
  ```
- [`EventOwner.generation`](../app/runtime/events.py#L52) — L52

  ```python
  EventOwner.generation(self) -> int
  ```
- [`EventOwner.scope`](../app/runtime/events.py#L55) — L55

  ```python
  EventOwner.scope(self) -> EffectScope
  ```
- [`EventOwner.can_dispatch`](../app/runtime/events.py#L57) — L57

  ```python
  EventOwner.can_dispatch(self) -> bool
  ```
- [`EventOwner.acquire_event_pin`](../app/runtime/events.py#L59) — L59

  ```python
  EventOwner.acquire_event_pin(self, registration_id: str) -> EventPin
  ```
- [`EventOwner.dispatch_context`](../app/runtime/events.py#L61) — L61

  ```python
  EventOwner.dispatch_context(self, selected_owners: tuple[str, ...]) -> AbstractContextManager[Any]
  ```
- [`ListenerHandle.in_flight`](../app/runtime/events.py#L83) — L83

  ```python
  ListenerHandle.in_flight(self) -> int
  ```
- [`ListenerHandle.unregister`](../app/runtime/events.py#L86) — L86

  ```python
  ListenerHandle.unregister(self) -> None
  ```
- [`ListenerHandle.close`](../app/runtime/events.py#L92) — L92

  ```python
  async ListenerHandle.close(self) -> None
  ```
- [`_Snapshot.release`](../app/runtime/events.py#L117) — L117

  ```python
  _Snapshot.release(self) -> None
  ```
- [`EventBus.on`](../app/runtime/events.py#L136) — L136

  ```python
  EventBus.on(self, owner: EventOwner, key: EventKey[Any], callback: Callable[..., Any], *, prepend: bool=False) -> ListenerHandle
  ```
- [`EventBus.emit`](../app/runtime/events.py#L308) — L308

  ```python
  EventBus.emit(self, key: EventKey[Any], *args: Any) -> None
  ```
- [`EventBus.bail`](../app/runtime/events.py#L312) — L312

  ```python
  EventBus.bail(self, key: EventKey[Any], *args: Any) -> Continue | Stop[Any]
  ```
- [`EventBus.parallel`](../app/runtime/events.py#L316) — L316

  ```python
  async EventBus.parallel(self, key: EventKey[Any], *args: Any) -> None
  ```
- [`EventBus.serial`](../app/runtime/events.py#L320) — L320

  ```python
  async EventBus.serial(self, key: EventKey[Any], *args: Any) -> Continue | Stop[Any]
  ```
- [`EventBus.waterfall`](../app/runtime/events.py#L324) — L324

  ```python
  EventBus.waterfall(self, key: EventKey[Any], *args: Any, next: Callable[..., Any]) -> Any
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`EventKey.__post_init__`](../app/runtime/events.py#L26) — L26

  ```python
  EventKey.__post_init__(self) -> None
  ```
- [`ListenerHandle.__init__`](../app/runtime/events.py#L68) — L68

  ```python
  ListenerHandle.__init__(self, bus: EventBus, owner: EventOwner, key: EventKey[Any], callback: Callable[..., Any]) -> None
  ```
- [`ListenerHandle._dispose_registration`](../app/runtime/events.py#L103) — L103

  ```python
  async ListenerHandle._dispose_registration(self) -> None
  ```
- [`_Snapshot.__init__`](../app/runtime/events.py#L113) — L113

  ```python
  _Snapshot.__init__(self, listeners: tuple[ListenerHandle, ...], pins: list[EventPin]) -> None
  ```
- [`EventBus.__init__`](../app/runtime/events.py#L127) — L127

  ```python
  EventBus.__init__(self) -> None
  ```
- [`EventBus._check_key`](../app/runtime/events.py#L131) — L131

  ```python
  EventBus._check_key(self, key: EventKey[Any]) -> None
  ```
- [`EventBus.on.register`](../app/runtime/events.py#L152) — L152

  ```python
  EventBus.on.register() -> Callable[[], Any]
  ```
- [`EventBus._snapshot`](../app/runtime/events.py#L160) — L160

  ```python
  EventBus._snapshot(self, key: EventKey[Any], args: tuple[Any, ...]) -> _Snapshot
  ```
- [`EventBus._sync_result`](../app/runtime/events.py#L186) — L186

  ```python
  EventBus._sync_result(value: Any) -> Any
  ```
- [`EventBus._decision`](../app/runtime/events.py#L194) — L194

  ```python
  EventBus._decision(value: Any) -> Continue | Stop[Any]
  ```
- [`EventBus._settle`](../app/runtime/events.py#L200) — L200

  ```python
  async EventBus._settle(task: asyncio.Task[Any]) -> Any
  ```
- [`EventBus._sync_dispatch`](../app/runtime/events.py#L217) — L217

  ```python
  EventBus._sync_dispatch(self, key: EventKey[Any], args: tuple[Any, ...], terminal: Callable[..., Any] | None) -> Any
  ```
- [`EventBus._sync_dispatch.invoke`](../app/runtime/events.py#L223) — L223

  ```python
  EventBus._sync_dispatch.invoke(listener: ListenerHandle, *values: Any) -> Any
  ```
- [`EventBus._sync_dispatch.chain`](../app/runtime/events.py#L230) — L230

  ```python
  EventBus._sync_dispatch.chain() -> Any
  ```
- [`EventBus._async_dispatch`](../app/runtime/events.py#L248) — L248

  ```python
  async EventBus._async_dispatch(self, key: EventKey[Any], args: tuple[Any, ...], terminal: Callable[..., Any] | None=None) -> Any
  ```
- [`EventBus._async_dispatch.invoke`](../app/runtime/events.py#L255) — L255

  ```python
  async EventBus._async_dispatch.invoke(listener: ListenerHandle, *values: Any) -> Any
  ```
- [`EventBus._async_dispatch.run`](../app/runtime/events.py#L260) — L260

  ```python
  async EventBus._async_dispatch.run() -> Any
  ```
- [`EventBus._async_dispatch.run.captured`](../app/runtime/events.py#L263) — L263

  ```python
  async EventBus._async_dispatch.run.captured(listener: ListenerHandle) -> BaseException | None
  ```
- [`EventBus._async_dispatch.run.chain`](../app/runtime/events.py#L285) — L285

  ```python
  async EventBus._async_dispatch.run.chain() -> Any
  ```
- [`EventBus._mode`](../app/runtime/events.py#L331) — L331

  ```python
  EventBus._mode(key: EventKey[Any], expected: DispatchMode) -> None
  ```

</details>

### app/runtime/host.py

可信组件注册、依赖重协调、generation、调用 pin、作用域、受监督任务与关闭屏障。

[打开源码](../app/runtime/host.py) · 1048 行

类：[`_Binding`](../app/runtime/host.py#L42)；[`_Record`](../app/runtime/host.py#L63)；[`_WaitingDependency`](../app/runtime/host.py#L95)；[`_Pin`](../app/runtime/host.py#L99)；[`CallScope`](../app/runtime/host.py#L117)；[`ProviderHandle`](../app/runtime/host.py#L166)；[`PluginHandle`](../app/runtime/host.py#L211)；[`ComponentContext`](../app/runtime/host.py#L282)；[`RuntimeHost`](../app/runtime/host.py#L507)。

主要可调用定义：

- [`_Binding.available`](../app/runtime/host.py#L52) — L52

  ```python
  _Binding.available(self) -> bool
  ```
- [`_Pin.release`](../app/runtime/host.py#L107) — L107

  ```python
  _Pin.release(self) -> None
  ```
- [`CallScope.require`](../app/runtime/host.py#L150) — L150

  ```python
  CallScope.require(self, key: ServiceKey[Any]) -> Any
  ```
- [`CallScope.identities`](../app/runtime/host.py#L156) — L156

  ```python
  CallScope.identities(self) -> tuple[BindingIdentity, ...]
  ```
- [`ProviderHandle.identity`](../app/runtime/host.py#L172) — L172

  ```python
  ProviderHandle.identity(self) -> BindingIdentity
  ```
- [`ProviderHandle.update_value`](../app/runtime/host.py#L175) — L175

  ```python
  ProviderHandle.update_value(self, value: Any) -> None
  ```
- [`ProviderHandle.notify`](../app/runtime/host.py#L181) — L181

  ```python
  ProviderHandle.notify(self) -> None
  ```
- [`ProviderHandle.request_close`](../app/runtime/host.py#L186) — L186

  ```python
  ProviderHandle.request_close(self) -> asyncio.Task[CloseReport]
  ```
- [`ProviderHandle.close`](../app/runtime/host.py#L201) — L201

  ```python
  async ProviderHandle.close(self, timeout: float | None=10) -> CloseReport
  ```
- [`PluginHandle.owner_id`](../app/runtime/host.py#L216) — L216

  ```python
  PluginHandle.owner_id(self) -> str
  ```
- [`PluginHandle.name`](../app/runtime/host.py#L220) — L220

  ```python
  PluginHandle.name(self) -> str
  ```
- [`PluginHandle.state`](../app/runtime/host.py#L224) — L224

  ```python
  PluginHandle.state(self) -> ComponentState
  ```
- [`PluginHandle.generation`](../app/runtime/host.py#L228) — L228

  ```python
  PluginHandle.generation(self) -> int
  ```
- [`PluginHandle.change_revision`](../app/runtime/host.py#L232) — L232

  ```python
  PluginHandle.change_revision(self) -> int
  ```
- [`PluginHandle.context`](../app/runtime/host.py#L236) — L236

  ```python
  PluginHandle.context(self) -> ComponentContext | None
  ```
- [`PluginHandle.request_close`](../app/runtime/host.py#L239) — L239

  ```python
  PluginHandle.request_close(self) -> asyncio.Task[None]
  ```
- [`PluginHandle.close`](../app/runtime/host.py#L251) — L251

  ```python
  async PluginHandle.close(self, timeout: float | None=10) -> CloseReport
  ```
- [`PluginHandle.restart`](../app/runtime/host.py#L257) — L257

  ```python
  async PluginHandle.restart(self, timeout: float | None=10) -> ReadyReport
  ```
- [`PluginHandle.update_config`](../app/runtime/host.py#L269) — L269

  ```python
  PluginHandle.update_config(self, config: Mapping[str, Any]) -> None
  ```
- [`PluginHandle.await_ready`](../app/runtime/host.py#L278) — L278

  ```python
  async PluginHandle.await_ready(self, timeout: float | None=10) -> ReadyReport
  ```
- [`ComponentContext.owner_id`](../app/runtime/host.py#L297) — L297

  ```python
  ComponentContext.owner_id(self) -> str
  ```
- [`ComponentContext.scope`](../app/runtime/host.py#L301) — L301

  ```python
  ComponentContext.scope(self) -> EffectScope
  ```
- [`ComponentContext.events`](../app/runtime/host.py#L308) — L308

  ```python
  ComponentContext.events(self) -> Any
  ```
- [`ComponentContext.config`](../app/runtime/host.py#L312) — L312

  ```python
  ComponentContext.config(self) -> Mapping[str, Any]
  ```
- [`ComponentContext.require`](../app/runtime/host.py#L370) — L370

  ```python
  ComponentContext.require(self, key: ServiceKey[Any]) -> Any
  ```
- [`ComponentContext.get`](../app/runtime/host.py#L376) — L376

  ```python
  ComponentContext.get(self, key: ServiceKey[Any]) -> Any
  ```
- [`ComponentContext.provide`](../app/runtime/host.py#L380) — L380

  ```python
  ComponentContext.provide(self, key: ServiceKey[Any], value: Any, check: Callable[[], bool] | None=None) -> ProviderHandle
  ```
- [`ComponentContext.effect`](../app/runtime/host.py#L401) — L401

  ```python
  ComponentContext.effect(self, setup: Callable[[], Any], label: str='effect', **kwargs: Any) -> Any
  ```
- [`ComponentContext.on_stop`](../app/runtime/host.py#L405) — L405

  ```python
  ComponentContext.on_stop(self, callback: Callable[[], Any]) -> None
  ```
- [`ComponentContext.spawn`](../app/runtime/host.py#L410) — L410

  ```python
  ComponentContext.spawn(self, awaitable: Awaitable[Any], *, name: str | None=None) -> asyncio.Task[Any]
  ```
- [`ComponentContext.plugin`](../app/runtime/host.py#L445) — L445

  ```python
  ComponentContext.plugin(self, spec: PluginSpec) -> PluginHandle
  ```
- [`ComponentContext.isolate`](../app/runtime/host.py#L449) — L449

  ```python
  ComponentContext.isolate(self, key: ServiceKey[Any], label: str | None=None) -> ComponentContext
  ```
- [`ComponentContext.intercept`](../app/runtime/host.py#L454) — L454

  ```python
  ComponentContext.intercept(self, key: ServiceKey[Any], config: Mapping[str, Any]) -> ComponentContext
  ```
- [`ComponentContext.config_for`](../app/runtime/host.py#L459) — L459

  ```python
  ComponentContext.config_for(self, key: ServiceKey[Any], *, base: Mapping[str, Any] | None=None, head: Mapping[str, Any] | None=None, merge: Callable[..., Any] | None=None) -> Any
  ```
- [`ComponentContext.call`](../app/runtime/host.py#L475) — L475

  ```python
  ComponentContext.call(self, *keys: ServiceKey[Any]) -> CallScope
  ```
- [`ComponentContext.can_dispatch`](../app/runtime/host.py#L478) — L478

  ```python
  ComponentContext.can_dispatch(self) -> bool
  ```
- [`ComponentContext.acquire_event_pin`](../app/runtime/host.py#L488) — L488

  ```python
  ComponentContext.acquire_event_pin(self, registration_id: str) -> _Pin
  ```
- [`ComponentContext.dispatch_context`](../app/runtime/host.py#L495) — L495

  ```python
  ComponentContext.dispatch_context(self, selected_owners: tuple[str, ...]) -> Any
  ```
- [`ComponentContext.on`](../app/runtime/host.py#L502) — L502

  ```python
  ComponentContext.on(self, key: Any, callback: Callable[..., Any], **kwargs: Any) -> Any
  ```
- [`RuntimeHost.load`](../app/runtime/host.py#L526) — L526

  ```python
  RuntimeHost.load(self, spec: PluginSpec, *, context: ComponentContext | None=None) -> PluginHandle
  ```
- [`RuntimeHost.call`](../app/runtime/host.py#L830) — L830

  ```python
  RuntimeHost.call(self, *keys: ServiceKey[Any]) -> CallScope
  ```
- [`RuntimeHost.is_ready`](../app/runtime/host.py#L895) — L895

  ```python
  RuntimeHost.is_ready(self, required: Sequence[PluginHandle | ServiceKey[Any]] | None=None) -> bool
  ```
- [`RuntimeHost.await_ready`](../app/runtime/host.py#L898) — L898

  ```python
  async RuntimeHost.await_ready(self, required: Sequence[PluginHandle | ServiceKey[Any]] | None=None, timeout: float | None=10) -> ReadyReport
  ```
- [`RuntimeHost.request_close`](../app/runtime/host.py#L992) — L992

  ```python
  RuntimeHost.request_close(self) -> tuple[asyncio.Task[None], ...]
  ```
- [`RuntimeHost.close`](../app/runtime/host.py#L999) — L999

  ```python
  async RuntimeHost.close(self, timeout: float | None=10) -> CloseReport
  ```
- [`RuntimeHost.retire`](../app/runtime/host.py#L1005) — L1005

  ```python
  async RuntimeHost.retire(self, handle: PluginHandle, timeout: float | None=10) -> CloseReport
  ```
- [`RuntimeHost.restart`](../app/runtime/host.py#L1008) — L1008

  ```python
  async RuntimeHost.restart(self, handle: PluginHandle, timeout: float | None=10) -> ReadyReport
  ```
- [`RuntimeHost.replace`](../app/runtime/host.py#L1011) — L1011

  ```python
  async RuntimeHost.replace(self, handle: PluginHandle, spec: PluginSpec, timeout: float | None=10) -> PluginHandle
  ```
- [`RuntimeHost.snapshot`](../app/runtime/host.py#L1017) — L1017

  ```python
  RuntimeHost.snapshot(self) -> dict[str, Any]
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`_disposed_setup`](../app/runtime/host.py#L37) — L37

  ```python
  _disposed_setup(context: Any) -> None
  ```
- [`_Pin.__init__`](../app/runtime/host.py#L100) — L100

  ```python
  _Pin.__init__(self, host: RuntimeHost, owner: _Record, binding: _Binding | None=None) -> None
  ```
- [`CallScope.__init__`](../app/runtime/host.py#L118) — L118

  ```python
  CallScope.__init__(self, host: RuntimeHost, keys: Sequence[ServiceKey[Any]], context: ComponentContext | None=None)
  ```
- [`CallScope.__aenter__`](../app/runtime/host.py#L125) — L125

  ```python
  async CallScope.__aenter__(self) -> CallScope
  ```
- [`CallScope.__aexit__`](../app/runtime/host.py#L159) — L159

  ```python
  async CallScope.__aexit__(self, *args: Any) -> None
  ```
- [`ProviderHandle.__init__`](../app/runtime/host.py#L167) — L167

  ```python
  ProviderHandle.__init__(self, host: RuntimeHost, binding: _Binding)
  ```
- [`ProviderHandle._close`](../app/runtime/host.py#L194) — L194

  ```python
  async ProviderHandle._close(self, affected: set[_Record]) -> CloseReport
  ```
- [`PluginHandle.__init__`](../app/runtime/host.py#L212) — L212

  ```python
  PluginHandle.__init__(self, record: _Record)
  ```
- [`PluginHandle.request_close.finished`](../app/runtime/host.py#L242) — L242

  ```python
  async PluginHandle.request_close.finished() -> None
  ```
- [`ComponentContext.__init__`](../app/runtime/host.py#L283) — L283

  ```python
  ComponentContext.__init__(self, host: RuntimeHost, record: _Record | None=None, generation: int=0, realms: Mapping[ServiceKey[Any], str] | None=None, configs: Mapping[ServiceKey[Any], tuple[Mapping[str, Any], ...]] | None=None) -> None
  ```
- [`ComponentContext._realm`](../app/runtime/host.py#L315) — L315

  ```python
  ComponentContext._realm(self, key: ServiceKey[Any]) -> str
  ```
- [`ComponentContext._assert_current`](../app/runtime/host.py#L318) — L318

  ```python
  ComponentContext._assert_current(self, *, registration: bool=False) -> None
  ```
- [`ComponentContext._resolve`](../app/runtime/host.py#L329) — L329

  ```python
  ComponentContext._resolve(self, key: ServiceKey[Any]) -> _Binding | None
  ```
- [`ComponentContext.spawn.owned_task`](../app/runtime/host.py#L422) — L422

  ```python
  async ComponentContext.spawn.owned_task() -> Any
  ```
- [`ComponentContext.spawn.completed`](../app/runtime/host.py#L434) — L434

  ```python
  ComponentContext.spawn.completed(done: asyncio.Task[Any]) -> None
  ```
- [`RuntimeHost.__init__`](../app/runtime/host.py#L508) — L508

  ```python
  RuntimeHost.__init__(self, *, history_limit: int=256, observation_limit: int=256) -> None
  ```
- [`RuntimeHost._signal`](../app/runtime/host.py#L539) — L539

  ```python
  RuntimeHost._signal(self) -> None
  ```
- [`RuntimeHost._wait_change`](../app/runtime/host.py#L542) — L542

  ```python
  async RuntimeHost._wait_change(self) -> None
  ```
- [`RuntimeHost._lookup`](../app/runtime/host.py#L546) — L546

  ```python
  RuntimeHost._lookup(self, key: ServiceKey[Any], realm: str) -> _Binding | None
  ```
- [`RuntimeHost._targets`](../app/runtime/host.py#L550) — L550

  ```python
  RuntimeHost._targets(self, record: _Record) -> dict[ServiceKey[Any], _Binding] | None
  ```
- [`RuntimeHost._observe`](../app/runtime/host.py#L561) — L561

  ```python
  RuntimeHost._observe(self, record: _Record, error: BaseException, phase: str) -> RuntimeFailure
  ```
- [`RuntimeHost._remember_disposed`](../app/runtime/host.py#L568) — L568

  ```python
  RuntimeHost._remember_disposed(self, record: _Record) -> None
  ```
- [`RuntimeHost._fail`](../app/runtime/host.py#L588) — L588

  ```python
  RuntimeHost._fail(self, record: _Record, error: BaseException, phase: str) -> None
  ```
- [`RuntimeHost._reconcile`](../app/runtime/host.py#L593) — L593

  ```python
  RuntimeHost._reconcile(self) -> None
  ```
- [`RuntimeHost._start`](../app/runtime/host.py#L612) — L612

  ```python
  RuntimeHost._start(self, record: _Record, targets: dict[ServiceKey[Any], _Binding]) -> None
  ```
- [`RuntimeHost._activate`](../app/runtime/host.py#L646) — L646

  ```python
  async RuntimeHost._activate(self, record: _Record, revision: int) -> None
  ```
- [`RuntimeHost._dependents`](../app/runtime/host.py#L672) — L672

  ```python
  RuntimeHost._dependents(self, record: _Record) -> set[_Record]
  ```
- [`RuntimeHost._closure`](../app/runtime/host.py#L680) — L680

  ```python
  RuntimeHost._closure(self, records: set[_Record]) -> set[_Record]
  ```
- [`RuntimeHost._withdraw_binding`](../app/runtime/host.py#L691) — L691

  ```python
  RuntimeHost._withdraw_binding(self, binding: _Binding) -> None
  ```
- [`RuntimeHost._invalidate_consumers`](../app/runtime/host.py#L699) — L699

  ```python
  RuntimeHost._invalidate_consumers(self, binding: _Binding) -> set[_Record]
  ```
- [`RuntimeHost._invalidate`](../app/runtime/host.py#L705) — L705

  ```python
  RuntimeHost._invalidate(self, records: set[_Record]) -> set[_Record]
  ```
- [`RuntimeHost._begin_stop`](../app/runtime/host.py#L732) — L732

  ```python
  RuntimeHost._begin_stop(self, record: _Record) -> None
  ```
- [`RuntimeHost._shutdown`](../app/runtime/host.py#L751) — L751

  ```python
  async RuntimeHost._shutdown(self, record: _Record) -> None
  ```
- [`RuntimeHost._wait_shutdowns`](../app/runtime/host.py#L817) — L817

  ```python
  async RuntimeHost._wait_shutdowns(self, records: set[_Record]) -> None
  ```
- [`RuntimeHost._check_wait`](../app/runtime/host.py#L825) — L825

  ```python
  RuntimeHost._check_wait(self, records: set[_Record]) -> None
  ```
- [`RuntimeHost._ready`](../app/runtime/host.py#L833) — L833

  ```python
  RuntimeHost._ready(self, required: Sequence[PluginHandle | ServiceKey[Any]] | None) -> ReadyReport
  ```
- [`RuntimeHost._dependency_failures`](../app/runtime/host.py#L874) — L874

  ```python
  RuntimeHost._dependency_failures(self, record: _Record, seen: set[_Record]) -> list[RuntimeFailure]
  ```
- [`RuntimeHost._providers_for`](../app/runtime/host.py#L885) — L885

  ```python
  RuntimeHost._providers_for(self, key: ServiceKey[Any], realm: str) -> tuple[_Record, ...]
  ```
- [`RuntimeHost._check_ready_wait`](../app/runtime/host.py#L914) — L914

  ```python
  RuntimeHost._check_ready_wait(self, required: Sequence[PluginHandle | ServiceKey[Any]] | None) -> None
  ```
- [`RuntimeHost._cycles`](../app/runtime/host.py#L937) — L937

  ```python
  RuntimeHost._cycles(self, roots: set[_Record] | None=None) -> tuple[str, ...]
  ```
- [`RuntimeHost._cycles.visit`](../app/runtime/host.py#L951) — L951

  ```python
  RuntimeHost._cycles.visit(record: _Record) -> tuple[str, ...]
  ```
- [`RuntimeHost._report`](../app/runtime/host.py#L971) — L971

  ```python
  RuntimeHost._report(self, records: set[_Record]) -> CloseReport
  ```
- [`RuntimeHost._await_close`](../app/runtime/host.py#L983) — L983

  ```python
  async RuntimeHost._await_close(self, records: set[_Record], timeout: float | None) -> CloseReport
  ```
- [`RuntimeHost._component_snapshot`](../app/runtime/host.py#L1030) — L1030

  ```python
  RuntimeHost._component_snapshot(self, record: _Record) -> dict[str, Any]
  ```

</details>

### app/runtime/leases.py

CallScope 的导出入口；实际租约状态由 host.py 单一实现维护。

[打开源码](../app/runtime/leases.py) · 6 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/storage/__init__.py

包初始化与公开符号导出。

[打开源码](../app/storage/__init__.py) · 1 行

无本地函数或类定义；可能是常量、导出或包占位。

### app/storage/types.py

精确标识字符串与 UTC 时间存储适配，统一数据库比较与时间语义。

[打开源码](../app/storage/types.py) · 89 行

类：[`UTCDateTime`](../app/storage/types.py#L14)；[`ExactString`](../app/storage/types.py#L48)；[`utc_now`](../app/storage/types.py#L70)。

主要可调用定义：

- [`UTCDateTime.load_dialect_impl`](../app/storage/types.py#L25) — L25

  ```python
  UTCDateTime.load_dialect_impl(self, dialect: Dialect) -> TypeEngine[datetime]
  ```
- [`UTCDateTime.process_bind_param`](../app/storage/types.py#L30) — L30

  ```python
  UTCDateTime.process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None
  ```
- [`UTCDateTime.process_result_value`](../app/storage/types.py#L38) — L38

  ```python
  UTCDateTime.process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None
  ```
- [`ExactString.load_dialect_impl`](../app/storage/types.py#L63) — L63

  ```python
  ExactString.load_dialect_impl(self, dialect: Dialect) -> TypeEngine[str]
  ```

<details>
<summary>内部方法、特殊方法与嵌套闭包</summary>

- [`ExactString.__init__`](../app/storage/types.py#L59) — L59

  ```python
  ExactString.__init__(self, length: int) -> None
  ```
- [`_utc_now_default`](../app/storage/types.py#L78) — L78

  ```python
  _utc_now_default(element: utc_now, compiler: SQLCompiler, **kw: object) -> str
  ```
- [`_utc_now_mysql`](../app/storage/types.py#L83) — L83

  ```python
  _utc_now_mysql(element: utc_now, compiler: SQLCompiler, **kw: object) -> str
  ```
- [`_utc_now_sqlite`](../app/storage/types.py#L88) — L88

  ```python
  _utc_now_sqlite(element: utc_now, compiler: SQLCompiler, **kw: object) -> str
  ```

</details>

## 4. 实际 HTTP 合同

来源为本次实际应用 OpenAPI；不是由设计文档猜测。原生路由静态注册，`native_auth_enabled=false` 时对应入口 404。

| 方法 | 路径 | operationId |
|---|---|---|
| GET | `/health` | `health_liveness` |
| GET | `/ready` | `runtime_readiness` |
| GET | `/api/v1/identity/me` | `identity_get_self` |
| GET | `/api/v1/identity/assignments` | `identity_list_self_assignments` |
| GET | `/api/v1/work-items/{work_item_id}` | `work_get_item` |
| PUT | `/api/v1/work-items/{work_item_id}/details` | `work_update_details` |
| GET | `/api/v1/work-items/{work_item_id}/operations/{idempotency_key}` | `work_get_operation` |
| GET | `/api/v1/account/sessions` | `account_list_sessions` |
| DELETE | `/api/v1/account/sessions/{session_ref}` | `account_revoke_session` |
| POST | `/api/v1/account/sessions/revoke-all` | `account_revoke_all_sessions` |
| GET | `/api/v1/account/consents` | `account_list_consents` |
| DELETE | `/api/v1/account/consents/{client_id}` | `account_revoke_consent` |
| POST | `/api/v1/account/password` | `account_change_password` |
| POST | `/api/v1/account/totp/reset` | `account_reset_totp` |
| GET | `/.well-known/openid-configuration` | `native_oidc_discovery` |
| GET | `/.well-known/oauth-authorization-server` | `native_oauth_metadata` |
| GET | `/oauth/jwks` | `native_jwks` |
| POST | `/oauth/token` | `native_token` |
| POST | `/oauth/revoke` | `native_revoke` |
| GET | `/oauth/userinfo` | `native_userinfo` |
| POST | `/oauth/userinfo` | `native_userinfo_post` |
| GET | `/oauth/authorize` | `native_authorize` |
| POST | `/oauth/login` | `native_browser_login` |
| GET | `/oauth/logout` | `native_logout_confirmation` |
| POST | `/oauth/logout` | `native_logout` |

## 5. 物理表清单

`tenant_*` 历史物理命名仍表示固定组织根，不是 SaaS 多租户入口。

```text
authentication_profile
authorization_resource
authorization_scope
bootstrap_record
command_execution
execution_audit
execution_outbox
execution_tenant_guard
external_identity
identity_control_operation
identity_provider
native_account
native_auth_event
native_authorization_code
native_browser_session
native_client
native_consent
native_login_request
native_login_throttle
native_logout_request
native_refresh_token
native_session
organization_human_authentication_policy
organization_root
outbox_delivery_attempt
permission_action
principal
relation_tuple
role_assignment
role_definition
role_version
role_version_action
tenant_authentication_binding
tenant_membership
trust_state
work_item
workload_registration
```

## 6. scripts 开发/运行工具函数

脚本是受控开发与验证入口，不是公开管理 API。列出模块层定义；内部实现见源码。

| 文件 | 类 | 顶层函数 |
|---|---|---|
| [scripts/authorization_admin.py](../scripts/authorization_admin.py) |  | `run()`, `main()` |
| [scripts/benchmark_human_work.py](../scripts/benchmark_human_work.py) | `BenchmarkCheckFailed`, `Sample` | `require()`, `database_url()`, `source_fingerprints()`, `observed_guard_block()`, `distribution()`, `instrument()`, `summary()`, `send()`, `batch()`, `owned_items()`, `state()`, `verify_writes()`, `harness()`, `edit_baseline()`, `cancellation_baseline()`, `revocation_baseline()`, `run()`, `main()` |
| [scripts/benchmark_mixed_auth_work.py](../scripts/benchmark_mixed_auth_work.py) | `Sample`, `DatabaseMeasurements`, `Actor` | `database_url()`, `validate_capacity_database()`, `source_digest()`, `distribution()`, `seed()`, `harness()`, `operate()`, `summarize()`, `workload()`, `login_burst()`, `run()`, `reset()`, `main()` |
| [scripts/check_authorization_migration.py](../scripts/check_authorization_migration.py) |  | `migrate()`, `append_and_check()`, `main()` |
| [scripts/check_browser_acceptance.py](../scripts/check_browser_acceptance.py) |  | `verify_report()`, `summary_report()`, `main()` |
| [scripts/check_contract.py](../scripts/check_contract.py) |  | `check_contract()`, `main()` |
| [scripts/check_database.py](../scripts/check_database.py) |  | `validate_test_database()`, `ensure_empty_database()`, `run_migration_cycle()`, `main()` |
| [scripts/check_distribution.py](../scripts/check_distribution.py) |  | `main()` |
| [scripts/check_redis.py](../scripts/check_redis.py) |  | `check_redis()` |
| [scripts/check_runtime_conformance.py](../scripts/check_runtime_conformance.py) |  | `verify()`, `main()` |
| [scripts/export_contract.py](../scripts/export_contract.py) |  | `canonicalize()`, `build_openapi()`, `parse_contract()`, `serialize_contract()`, `format_for_path()`, `write_contract()`, `main()` |
| [scripts/freeze_release.py](../scripts/freeze_release.py) |  | `git()`, `checksum()`, `migration_heads()`, `main()` |
| [scripts/identity_admin.py](../scripts/identity_admin.py) |  | `local_operator_ref()`, `run()`, `main()` |
| [scripts/identity_idp_prepare.py](../scripts/identity_idp_prepare.py) |  | `write_private()`, `main()` |
| [scripts/identity_idp_smoke.py](../scripts/identity_idp_smoke.py) | `LoginForm` | `assert_local_url()`, `authorization_code()`, `run()`, `main()` |
| [scripts/identity_mfa_prepare.py](../scripts/identity_mfa_prepare.py) |  | `fixture_documents()`, `main()` |
| [scripts/identity_mfa_smoke.py](../scripts/identity_mfa_smoke.py) | `FixtureForm`, `LoginAttempt` | `otp_code()`, `_form()`, `start_login()`, `submit_password()`, `submit_otp()`, `complete_otp()`, `authorization_result()`, `exchange_code()`, `profile()`, `login_tokens()`, `_verified()`, `_check_refresh()`, `run()`, `read_config()`, `main()` |
| [scripts/identity_mfa_upgrade.py](../scripts/identity_mfa_upgrade.py) |  | `_read_private()`, `_replace_private()`, `_request()`, `_get()`, `_exact()`, `_check_mappers()`, `_client()`, `_plan()`, `_ensure_owned()`, `_all_mappers()`, `_check_client()`, `upgrade()`, `main()` |
| [scripts/native_auth_admin.py](../scripts/native_auth_admin.py) | `RecoveryPlan` | `read_private_plan()`, `_write_private()`, `_new_signing_entry()`, `generate_keys()`, `prepare_key_rotation()`, `run()`, `main()` |
| [scripts/native_auth_smoke.py](../scripts/native_auth_smoke.py) |  | `hidden()`, `save_fixture()`, `login()`, `check()`, `main()` |
| [scripts/native_oauth_admin.py](../scripts/native_oauth_admin.py) |  | `_client_secret()`, `run()`, `main()` |
| [scripts/runtime_reference/run.py](../scripts/runtime_reference/run.py) |  | `run()` |
| [scripts/runtime_reference/shared_python.py](../scripts/runtime_reference/shared_python.py) | `Scenario` | `run()` |
| [scripts/smoke.py](../scripts/smoke.py) |  | `_parser()`, `main()` |

机器清单：[AST/导入/HTTP/表清单快照](reviews/stage-architecture-inventory-2026-10-26.json)。源码链接遵循 Markdown `#L` 定位习惯；不同查看器若不支持行定位，打开文件后按标记的 L 行号查看。
