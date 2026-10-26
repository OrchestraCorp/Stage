---
title: Maestro 与 Stage 的 P1b 联调合同
status: ready-for-client-integration
review_date: 2026-10-26
policy_effect: none
---

# Maestro 与 Stage 的 P1b 联调合同

## 2026-10-26 管理中心归属补充

用户已确认完整人员、组织、角色／任职、账户安全、应用认证配置与审计管理体验融入Maestro。Stage提供正式管理API与安全执行，云端和本地分别实现后共同验收。完整页面范围、接口准备项和恢复要求见[管理中心合同边界](maestro-stage-administration.md)。原有25个HTTP操作之后，首个管理切片新增9个操作，见[管理接口合同](human-administration-first-slice.md)；以下本人登录／任职与工作项合同不因此扩大为已完成的管理员产品。

## 2026-10-26 原生登录合同补充

现行开发方向采用[最小原生认证](native-authentication-design.md)。Maestro 通过系统浏览器访问固定 issuer 的 `/oauth/authorize`，使用 `response_type=code`、`scope=openid`、唯一 `state`/`nonce`、PKCE S256 和预登记的精确回调。浏览器完成密码＋TOTP；首次绑定和恢复进入同一认证流程。客户端收到 code 后核验 state，向 `/oauth/token` 交换；验证 ID token 的 issuer/audience/nonce，调用业务接口使用 access token。现已支持顶层导航 SSO、prompt/max_age、用户同意、标准退出和受控多客户端；公开自动注册与跨域 SPA CORS 不在范围。详见[功能矩阵](oauth-functional-coverage.md)。

本地 profile 为 `stage-native-human@1`；access token 的 `typ=at+jwt`、audience=`stage-api`，ACR=`urn:orchestra:acr:pwd-totp`、AMR=`pwd,otp`。任职发现和选择规则不变，不从 ID token 或 token 内角色推导权限。具体 issuer/client/redirect 从部署配置取得，不硬编码开发虚构账号；密钥、密码、TOTP 和恢复码不随文档交付。

token/revoke 请求不要继承业务 API 的 Bearer 头；public 发送表单 client_id，confidential 使用 HTTP Basic。

refresh 必须在客户端串行消费并原子替换保存；旧 refresh 重用会撤销整条会话族。网络超时后不能盲重试旧 code/refresh；结果不确定时重新交互登录。调用 `/oauth/revoke` 可撤销该会话，之后原 access token 访问 Stage 立即被拒绝。组织 MFA 默认新鲜度与部署策略一致，刷新不会延长原认证时间，收到 `MFA_REQUIRED` 须重新登录。

账户安全使用 `/api/v1/account/password` 与 `/api/v1/account/totp/reset`，需要最近 300 秒的原生强认证；变更后退出全部会话并重新登录。因素重置响应已收紧为 200 `{credential_version, enrollment_code}`：必须保存仅返回一次的重绑码，重新密码登录后在恢复表单使用该码；密码单独不能重新领取 seed。网络结果未知时不能用旧令牌重取码，需按受控恢复处理。公开变更不接受客户端指定人员或组织。实际桌面回调监听、系统浏览器、OS 安全存储、并发刷新互斥与恢复交互仍由 Maestro 联调验收。完整参数见[OpenAPI](../contracts/openapi.yaml)，服务端操作见[原生运行手册](../deployment/identity/native-authentication.md)。

以下 P1b 原合同继续约束人员业务接口；涉及 Keycloak 测试 fixture 的段落是该轮历史接入材料。

本合同面向本地开发的 Maestro 与云设备上的 Stage。Stage 侧已支持人员认证、本人任职发现、组织统一 MFA 策略、当前单任职授权及已有工作项标题/说明编辑；实际桌面登录、回调、token 保管和交互验收由客户端联调完成。当前不提供完整人员管理或任务产品，整体 P1b 尚未退出；本合同覆盖密码＋TOTP 登录与本人任职选择接缝，服务端和真实隔离协议证据不替代 Maestro 实际交互、因素绑定/恢复或生产验收。字段以 [OpenAPI](../contracts/openapi.yaml) 为准，本轮证据见 [P1b.4](reviews/p1b4-assignments-mfa-2026-10-26.md)，历史 I1 范围见 [覆盖矩阵](reviews/p1b-identity-coverage-2026-10-25.md)。

## 1. 登录与部署配置

**下表仅记录早期外部 Keycloak fixture，不能作为当前原生登录的默认配置。** 当前原生 profile 为 `stage-native-human@1`，其 issuer、client 和回调取自部署登记，见上方原生登录合同与[原生运行手册](../deployment/identity/native-authentication.md)。

| 配置 | 历史外部 Keycloak 隔离环境 |
|---|---|
| Stage base URL | 云设备 `http://127.0.0.1:8080`；本地使用明确的 SSH 转发 |
| 精确 issuer | `http://127.0.0.1:18080/realms/stage-development` |
| 人员 public client | `maestro-stage-dev`，授权码＋PKCE S256，无 client secret |
| 精确回调 | `http://127.0.0.1:18765/callback` |
| Stage audience | `stage-api` |
| 人员认证选择 | `profile_ref=local-keycloak-human`、`profile_version=1` |
| 当轮人员登录强度 | 密码＋TOTP，`acr_values=mfa`；Stage policy required/version 2，最大认证年龄 900 秒 |

本地转发需让客户端实际可达的 issuer 与已登记 issuer 保持一致；不能把 token 中的 issuer 改成另一主机名，也不能让 Stage 按 token 自报地址发现信任源。回调、client 和发行者变更必须重新明确登记。上述 loopback HTTP 仅属开发验收，生产入口和代理拓扑单独验收。当前未配置浏览器跨域白名单；Web/BFF 不自动继承 Desktop 拓扑。

客户端使用系统浏览器登录，验证 state、精确回调、PKCE 和自身 ID token 的 nonce/签名/issuer/audience。只把面向 Stage 的 access token 作为 Bearer 发送给 Stage；ID token、refresh token、角色字段和 Agent ID 都不能建立业务权限。refresh token 留在客户端的安全凭据边界，不交给模型、WASM、业务队列或 Stage 工作项 API。

用户选定密码＋TOTP。当前原生认证模块执行真实因素验证，Stage 的组织策略统一限制 human 的精确 profile/version、ACR、AMR 和 auth_time 年龄。早期外部 IdP 验证见[P1b.4 部署记录](reviews/p1b4-assignments-mfa-2026-10-26.md)，当前原生配置按[原生运行手册](../deployment/identity/native-authentication.md)核验；数据库升级本身不会启用 MFA。

Maestro 从可信部署配置获得要求的 ACR 和认证年龄，在授权码＋PKCE 浏览器流程发送相应 `acr_values` / `max_age`，按 IdP 合同完成密码与 TOTP 输入。收到 `MFA_REQUIRED` 时发起所需的重新认证，不只刷新 token，也不自动切换旧 profile。刷新后的 `iat` 不重置 `auth_time`；刷新 token 不能让弱会话变强。MFA 操作说明见[开发管理](login-mfa-development.md)。

脚本使用预配置虚构认证器不等于用户已完成扫码绑定；真实桌面的成功、取消、OTP 失败、认证过期和恢复仍须验收。业务批准的 WebAuthn / IdP step-up 另按 P1c/P1d 绑定意图，登录成功不自动产生批准。

## 2. 最小调用顺序

1. 完成人员登录，以 Bearer 调用 `GET /api/v1/identity/me?profile_ref=stage-native-human&profile_version=1`。示例使用当前原生 profile，实际值必须匹配部署登记；全链路使用同一选定 profile/version。
2. 检查 `principal_type=human`，获取当前主体、组织根、成员经历。响应不是可重用权限票据；`authentication_valid_until` 不表示其间权限不会被撤销。
3. 调用 `GET /api/v1/identity/assignments?profile_ref=stage-native-human&profile_version=1&limit=20`，展示返回的任职名称和 scope，由本人明确选择一个 `assignment_ref`。有 `next_after` 时携其继续查询；即使本页 items 为空也不能提前结束。工作项列表/创建仍未提供，`work_item_id` 由显式联调 fixture 提供；不从角色名称或 scope 拼任职。
4. 调用工作项 GET 获取最新 `version`，然后按下表提交编辑。
5. 保存原幂等键和原请求输入，按结果状态决定显示、刷新或恢复；同一操作重新认证不会更换原幂等键。

当前常驻开发库未自动授予角色或建立 WorkItem。返回有效 `/identity/me` 后，访问工作项仍可能是 403。这是身份与业务权限分开的预期行为。真实写验证使用 [独立测试库 fixture](../tests/integration/test_human_work_live.py)，不会给常驻开发库开权。首次人工联调的数据由开发管理员按 [有限授权登记](authorization-development.md) 显式安装。

### 本人任职投影

请求除 `profile_ref/profile_version` 外只接受 `limit`（默认 20，最大 50）和 `after`；不接受人员或组织选择。成功返回：

```json
{
  "items": [
    {
      "assignment_ref": "assignment:engineering-owner",
      "role_ref": "role:engineering-owner",
      "role_revision": 1,
      "role_code": "engineering-owner",
      "role_display_name": "工程负责人",
      "scope_ref": "scope:engineering",
      "valid_from": "2026-10-26T00:00:00Z",
      "expires_at": null,
      "version": 1
    }
  ],
  "next_after": null
}
```

响应只列出当前人员、固定成员经历下的有效任职，并验证角色内容和 scope；没有任职时正常返回空列表，不自动创建岗位或合并权限。有界候选分页可以在过滤失效角色/scope 后返回空 items 和非空 next_after；翻页期间数据可变化，客户端可去重或刷新，不把它当完整快照。选项也不是权限票据，业务读取、写入和结果恢复仍重新检查当前任职。

## 3. 工作项合同

所有工作项请求均带 Bearer，以及 query `profile_ref`、`profile_version`、`assignment_ref`。组织根由服务端固定；额外 query（包括 `tenant_ref`、`organization_ref`、`actor_ref`）返回 422。

| 请求 | 行为 |
|---|---|
| `GET /api/v1/work-items/{id}` | 当前获授权的工作项投影，包含 title、description、lifecycle、classification、version |
| `PUT /api/v1/work-items/{id}/details` | 完整替换标题/说明，必须带 `Idempotency-Key` 与下方正文 |
| `GET /api/v1/work-items/{id}/operations/{key}` | 恢复当前人员自己的原操作结果；仍检查当前读权限 |

PUT 正文示例（对象及任职来自已登记的合成测试数据）：

```json
{
  "title": "更新后的标题",
  "description": "更新后的说明",
  "expected_version": 1
}
```

标题去首尾空白后为 1～256 字符；说明必须显式传文本（最多 20,000 字符）或 `null`。规范化后的标题/说明 JSON 另受命令内核 64 KiB 预算限制，包含大量多字节字符的文本可能先达到此限制并返回 422。版本必须为正 int32，不能传布尔或字符串。不能附带状态、scope、分类、角色或人工确认字段；NUL 等无效标识/文本在输入边界拒绝。

成功结果为 `{execution_id, status: "succeeded", result, replayed, completed_at}`。`result.version` 是这次提交生成的版本，历史操作结果不保证等于对象最新版本。需要继续编辑时重新 GET 工作项。

幂等键采用每个业务意图生成的 UUID 等受限 ASCII 字符串，长度 1～128，首字符为字母/数字，其余允许字母/数字和 `._:-`。它在同一组织、人员和命令版本内唯一，不能对不同工作项重复使用同一个键。重试保持原目标、原 expected_version、原输入和原任职；新操作才生成新键。

刷新凭据后，同一人员可以重放原操作，原审计不变。PUT 换任职可能返回幂等冲突；GET 恢复允许同一人员选择另一个当前有 `task.view` 的任职，不会因此再次执行。另一人员即使可读同一工作项，也不能读取原人员的操作结果。

## 4. 错误与恢复

所有响应带 `X-Request-ID`；身份与工作项响应均 `Cache-Control: no-store`。错误结构为 `{error: {code, message, request_id}}`。客户端按 code/HTTP 状态处理，不解析英语 message 获取权限结论。

| 状态/代码 | 客户端处理 |
|---|---|
| 401 / `AUTHENTICATION_REQUIRED`、`INVALID_CREDENTIAL` | 获取当前有效人员凭据。若此前写入结果未知，仍使用原键恢复，不生成新操作 |
| 401 / `MFA_REQUIRED` | 读取 `WWW-Authenticate: Bearer error="insufficient_user_authentication"`，按可信部署配置重新发起密码＋TOTP；恢复原未知操作时保留原键和输入 |
| 403 / `WORK_ACCESS_DENIED`、身份拒绝 | 当前人员、成员、认证来源或任职/资源访问不允许；不自动切换其它岗位或无限刷新 token |
| 409 / `OBJECT_VERSION_CONFLICT` | 重新读取对象，让用户确认如何处理新版本；改变业务意图时使用新键 |
| 409 / `IDEMPOTENCY_CONFLICT` | 原键对应的目标、输入或任职不匹配；先核对原操作记录，不以换键自动重发掩盖冲突 |
| 409 / `WORK_DETAILS_CONFLICT` | 当前领域状态或版本上限不允许编辑，重新读取并停止盲重试 |
| 404 / `OPERATION_NOT_FOUND` | 暂无该人员/目标下可返回的原操作；不能据此断言之前请求一定未提交 |
| 422 | 修正输入和合同版本；不给重试分配新的业务意图 |
| 503 / `COMMIT_OUTCOME_UNKNOWN` | 使用响应的相对 `Location` 与当前凭据查询原结果，必要时以原键原输入重试 |
| 503 / `WORK_UNAVAILABLE`、网络超时或连接中断 | 服务/请求预算问题，不能统一解释为已回滚；先恢复原结果，避免重复业务操作 |

服务器整体命令预算默认 10 秒；事务取消和资源清理仍需完成。客户端取消等待不构成数据库回滚证明。恢复 Location 不含 token，只是普通受保护读接口；客户端只向配置的 Stage origin 发送凭据。

## 5. 注销与撤权

客户端退出应清理自身凭据并按当前 issuer 合同结束会话。**当前原生认证会校验数据库中的会话和凭据状态，撤销提交后原 access token 访问 Stage 会被拒绝。** 单 token revoke 撤销对应会话族，浏览器退出撤销当前浏览器关联会话，本人“全部退出”撤销全部 RP/browser 会话及待完成登录；三者范围不同。第三方 RP 自身的本地登录状态不等于 Stage 会话，当前不提供 front/back-channel logout 通知。具体边界见[OAuth 功能矩阵](oauth-functional-coverage.md)。

仅对仍被部署显式接纳的**外部离线 JWT 来源**，Stage 未接入远端 logout/account-disabled 事件或 introspection，外部 IdP 注销不保证已签发 access token 立即在 Stage 失效。此类旧 token 仍受 exp、组织 MFA 策略的 auth_time 年龄及当前主体/成员/任职/profile 状态约束。内部 evidence TTL（默认 60 秒）不能当作远端撤销时限；同一尚未到期 JWT 能再次签发新的内部 evidence。

Stage 本地撤销与命令提交按共同 PostgreSQL 锁序协调；撤销提交后新请求、重放及结果读取重新判定权限。当前 dev/test 工具可以显式撤销主体、成员、profile 或任职；新增管理 API 只开放首管提名接受与清单内普通任职授撤，主体／成员停用和组合离职仍未交付。生产接入前须确定 access-token 最大寿命、外部撤销可见性及紧急阻断流程。

## 6. 联调退出记录

首轮最小路径联调至少记录：系统浏览器登录与回调、state/nonce 错配拒绝、真实 token 保管/刷新、本人任职分页/空页与明确选择、合法编辑、版本冲突、提交断线后原键恢复、撤权后的 403、退出后凭据清理。Stage 的协议脚本和自动测试已经提供服务端证据，但不替代这些桌面验收。

本人任职查询和登录 MFA 策略已实现并同步 OpenAPI；真实隔离 IdP、弱凭据拒绝、认证年龄、刷新与策略竞争有服务端证据。完整 P1b 身份退出还需在 Maestro 验证密码＋TOTP 成功、取消/失败、强度不足提示、认证过期和原键恢复，以及真实任职选择；因素扫码绑定/恢复与生产身份接入单独记录，不能以脚本登录代替用户交互。正式入离职/人员/权限管理和个人资料产品另定最小切片，开发安装工具不计为产品完成。

此次仅交付合同与云端实现，不修改本地 Maestro；不因工具辅助自动标记“人工已确认”，也不引入独立 Agent 成员、岗位或业务 API。
