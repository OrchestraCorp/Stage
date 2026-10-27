---
title: Stage 自研最小认证与 OAuth/OIDC 设计
status: implemented-service-side-verified
decision_date: 2026-10-26
policy_effect: none
---

# Stage 自研最小认证与 OAuth/OIDC 设计

用户已明确决定：按照 OrchestraCorp 的实际需求，自研最小认证模块，复用成熟密码学/协议库，完成实现与详细复测。此决定取代此前“长期默认依赖独立 Keycloak”的建议。Keycloak 暂保留为已验证的迁移对照；本轮不复制其通用身份联邦、LDAP、插件市场或完整身份管理控制台。

## 1. 职责与首版范围

自研认证模块负责本地账号、密码＋TOTP、会话、授权码＋PKCE、token 签发/刷新/撤销、公钥与最小 OIDC 身份协议。现有 IdentityService 继续负责发行来源准入、外部账号到 Principal 的精确映射、组织成员、MFA 策略；现有 authz 与 CommandKernel 继续决定任职、对象权限及业务事务。初期在 Stage 同进程部署，凭据数据与业务数据分表，运行服务通过 provider 管理资源。

当前多应用扩展以[完整性矩阵](oauth-functional-coverage.md)与[实施计划](oauth-completion-plan.md)为准。以下保留首版边界及后续收紧过程。

首版支持预登记的第一方 public 客户端：authorization_code＋PKCE S256、refresh_token、openid scope、固定 Stage API audience；仅精确登记的回调，禁止 implicit/password grant、动态客户端注册、自动 email 关联及任意 audience。OIDC Discovery/JWKS/UserInfo 用明确支持字段，尚不宣称官方一致性认证或全部 OAuth/OIDC 扩展兼容。

账号绑定既有 HUMAN Principal；部署操作者显式安装来源/profile、客户端与账号映射，不因登录自动加入公司或授予任职。迁移保留 Principal、membership 和 assignment 引用；新 issuer/subject 使用独立 namespace，任何账号关联都必须显式指向已核验人员。旧登录入口只在阶段迁移政策中保留，启用 native-only MFA allowlist 后旧 Keycloak profile不能绕过要求。

## 2. 凭据、登录与恢复

密码采用成熟 Argon2id 库，验证在有界 CPU 工作线程执行；不保留明文，禁止日志回显。TOTP 使用成熟库生成/验证，种子在数据库中加密，主密钥位于数据库外私有文件。成功使用的时间步原子推进，防止同一 OTP 被重复使用。首次绑定必须先证明密码并实际输入正确 OTP；提供一次性恢复码，仅返回一次、数据库只存哈希。

登录 HTML 使用 `Referrer-Policy: same-origin` 以保留真实浏览器表单 Origin；跨来源回调不发送 Referer，JSON 与重定向使用 `no-referrer`。浏览器登录使用有界、短期的持久认证事务，浏览器秘密 cookie 与表单 CSRF 绑定；验证过密码后才能展示/验证第二因素。认证事务绑定原客户端、redirect、state、nonce、PKCE 和期限；不把密码作为 OAuth password grant。错误因素、重放、过期、修改 CSRF/浏览器绑定均拒绝；错误响应不泄露账号存在性或凭据内容。登录尝试有数据库计数与冷却期限。

人员可在近期强认证下改密码；改密码、重置第二因素或停用账号递增凭据版本并撤销现有会话。丢失 TOTP 使用密码＋单次恢复码进入重新绑定，恢复码消费与旧会话撤销原子完成；只有新 TOTP 实际验证后才发行正常 MFA 凭据。没有恢复码的恢复由明确部署操作者的受控操作处理，不提供按邮箱无证据找回。

## 3. OAuth/OIDC 与会话

授权码短期、单次使用，以哈希落库，并绑定 client/redirect/PKCE；交换失败不得改绑其它账号。access JWT 与 ID token 明确区分用途及 audience；access token 有限寿命，ID token绑定 nonce。refresh token 为高熵不透明随机值，只存哈希，每次刷新轮换；检测旧 refresh 重用时撤销整条会话族，过期/停用/凭据版本变化均拒绝。并发刷新只允许一个成功，其它重用触发明确撤销，不偷偷产生并行有效族。

Stage 本地原生 access token 除验签外，还核验当前账号/会话/凭据版本；已签发 token 不因签名仍有效而绕过本地退出、密码变更或停用。业务事务复用 trust→principal→组织 guard 的锁序，凭据/会话变更与命令提交有可验证顺序。token刷新保留原 auth_time/ACR/AMR，不能抬高强度或延长原认证新鲜度。

RSA 签名和验证使用成熟库；私钥与 TOTP 加密主密钥放在部署外部私有文件。受控轮换保留需要验证的旧公钥；撤销 key 与轮换分开，旧会话/旧 policy 重验继续有效。生产 issuer 要求 HTTPS，开发仅允许明确 loopback HTTP；不能从 Host/Forwarded 头或 token 自报地址建立发行信任。

## 4. 持久与失败边界

最小表覆盖账号、客户端、浏览器认证事务、登录会话、授权码、refresh 凭据、认证事件和限流状态。认证状态在 PostgreSQL 持久保存，Redis 不承担凭据撤销或一次性使用的权威。密码、OTP、恢复码和 refresh 的明文不进入 URL；授权码仅通过精确登记的回调 URL 传递，客户端应交换后清理地址并关闭回调查询串日志。所有敏感材料均不得进入审计、日志和错误输出；恢复材料只在指定安全响应或私有文件中交付。

凭据校验、一次性 token 消费、会话变更和审计同事务；发行响应丢失后不宣称凭据未消费。授权码/refresh重复使用按安全协议拒绝或撤销，不能照搬业务命令的“返回旧明文token”幂等策略。迁移为增量；downgrade 拒绝删除已经产生的认证事实。

## 5. 实施与验收

1. 固定上述范围和代码/数据合同，安装成熟依赖并补迁移。
2. 实现凭据与会话、最小 OAuth/OIDC 浏览器流程、原生认证适配及管理操作。
3. 隔离测试证明 password/TOTP/CSRF/PKCE、因素恢复、密码变更、会话退出、refresh重用/并发、公钥轮换及错误输入边界。
4. PostgreSQL 真库验证一次性消费、会话撤销与业务提交竞争、迁移与原有回归；真实 HTTP/浏览器流程贯通任职发现和工作项写入。
5. 对本机虚构人员显式关联原 Principal，使用私有新凭据完成新 issuer 链路；验证后收紧组织策略。Keycloak 保留原数据和恢复对照，不无故重置旧身份。

执行结果写入独立 review，不能因本设计存在就宣称已成熟或已全部实现。Maestro 的实际系统浏览器、OS 凭据存储及生产恢复责任仍单独验收，但本轮须完成可运行的服务端与可复现端到端验证。

## 6. 已落地的实现边界

`app/native_auth` 保存凭据与 OAuth 服务；`app/providers/native_auth.py` 在显式配置启用时加载，持有 STORAGE lease 并约束请求期限；`app/api/native_auth.py` 提供静态协议路由和账户安全入口。`NativeAuthenticator` 负责原生 JWT profile，`IdentityService` 在原有 trust→principal→组织 guard 下重新核验 native session/account/version；原有任职、授权规则和 CommandKernel 不因新发行器改变。

迁移 `e8a1c6d9f240` 新增八张表，总计 34 张应用表：

| 表 | 持久事实 |
| --- | --- |
| `native_account` | 显式关联既有 HUMAN Principal、Argon2id 密码、加密因素、凭据版本、失败冷却 |
| `native_client` | 预登记 public client 与精确回调；首版安装固定一个 client/profile |
| `native_login_request` | 有期限的浏览器秘密/CSRF 哈希、密码/因素阶段、PKCE 与回调绑定 |
| `native_session` | 当前会话、原始认证时间、强度、固定期限和撤销状态 |
| `native_authorization_code` | 授权码哈希、PKCE、期限和消费事实 |
| `native_refresh_token` | refresh 哈希、会话族、期限和消费事实 |
| `native_auth_event` | 凭据变更与认证安全事件，不存原始秘密 |
| `native_login_throttle` | PostgreSQL 权威的地址尝试预算；账号另有失败锁定 |

访问 token 最长 5 分钟，会话族默认 8 小时；授权码 60 秒、浏览器认证请求 5 分钟。刷新保留原始 `auth_time`，组织新鲜度到期后须重新登录。改密码/因素重置要求最近 300 秒的原生强认证，锁后重验访问 token 与会话，提交前再校验期限。恢复码使用后立即撤销旧会话、消费该码，未消费的码可用于中断后重新开始；新因素验证成功时才生成新恢复码并废弃整批旧码。既有因素替换不再允许密码单独领取种子，完成新的真实 TOTP 验证后才恢复正常登录。

密钥文件由整个 Stage 进程启动时载入；候选密钥轮换须停止并排空旧进程、切换文件再整体启动。保留旧公钥使未过期 token 可验证，移除旧公钥需要独立、明确的部署决定；当前没有热更新 keyring、多节点传播或在线 trust/key 撤销接口。会话退出与凭据变更的本地即时失效已有数据库协议，不能据此推断旧 Keycloak access token 具备远端即时撤销。

服务端原生模块默认关闭；只有显式配置、登记来源/客户端及账号映射才开放。开发 CLI 的安装计划带幂等操作记录；密码重置/停用采用预期凭据版本和安全事件，提交未知后应先 inspect 核对，不能重放并返回旧明文秘密。生产管理入口、邮件找回、WebAuthn、外部 IdP 联邦、跨域 SPA、完整 OIDC 一致性认证与历史认证数据自动清理均未实现。运行方式见[本地认证手册](../deployment/identity/native-authentication.md)。

本轮基础实现、复测及常驻设备切换结果见[验收记录](reviews/native-authentication-2026-10-26.md)；企业受控多应用 OAuth/OIDC 的最终矩阵和复测见[OAuth 完整性矩阵](oauth-functional-coverage.md)。

## 7. 深度审计后的因素替换合同

2026-10-26 独立审计复现并修复了因素重绑抢占窗口，见[审计记录](reviews/native-authentication-audit-2026-10-26.md)。新增迁移 `b7f0d3a9216c`，表数仍为 34；`native_account.totp_bootstrap_allowed` 仅允许显式安装后的首次密码引导绑定，第一次绑定成功及任何恢复/重置后均为 false。迁移对已有种子或凭据版本大于 1 的账号回填 false，不能把未知历史重置态重新解释为首次绑定。

自助 `/api/v1/account/totp/reset` 仍要求近期强认证、密码和未使用的 OTP；现在返回 **200** `{credential_version, enrollment_code}`，仅这次响应提供重绑码。客户端保存该码，重新密码登录后在恢复表单中提交该码，才取得绑定新因素的权限。码只以哈希落库，不进入日志、审计或错误；旧会话已撤销，不能用旧请求再取一次。丢失重绑响应且无其他有效恢复证明时，须走操作者恢复，不能退回密码单因素。

开发 CLI 的因素重置必须提供新的 `--enrollment-output` 私有输出文件；控制台只报告版本及路径。恢复码消费后的 enroll 状态绑定当前浏览器请求、密码证明和凭据版本，其他仅持密码的请求无法领取种子。OTP/恢复在 flush 后再次检查浏览器请求和密码证明期限，过期则回滚因素、版本、安全事件与会话撤销。

issuer 必须是规范化浏览器 origin：不能包含路径、空 `?/#`、默认端口显式拼写、大写主机或非规范 IP。配置错误在启动阶段拒绝，不能通过接受任意 Origin 来兼容。工作项读取在最后一次数据库读取后重新检查完整身份/MFA/任职/关系授权，保护读取末尾的到期边界。

## 8. 企业受控多应用扩展

迁移 `c6f3a19d820b` 增加浏览器 SSO、同意记录和退出请求三张表，当前 37 张。客户端和 RP 会话增加版本、scope/audience、认证方式等固定事实；旧客户端迁移保留 stage-api/public/openid 语义，新应用默认 stage-userinfo 并要求同意。客户端配置变更或秘密轮换会撤销该客户端旧会话，重新启用不使旧令牌复活。

现已实现多客户端管理、SSO/同意、prompt/max_age、query/form_post、标准客户端认证、RP确认退出及本人会话/授权管理；选定能力及明确未包含项见[功能矩阵](oauth-functional-coverage.md)。会话新鲜度在最后一次数据库等待后仍需有效，当前组织策略收紧也会应用到在途授权；刷新保留原认证时间，不伪造新的 MFA。

本机 Uvicorn access 日志移除查询串，以免 GET logout 的 ID token hint 与授权 state 进入日志。RP callback 和入口代理同样需要独立配置不记录这些秘密。来源限流按登录因素/授权/退出发起分开，默认300次/600秒，可通过部署配置调整；账号失败锁定和哈希并发预算继续独立生效。

## 9. 实现职责与持续验证

原生认证 HTTP 的组合入口仍为 `app/api/native_auth.py`，具体实现位于 `api/native/`：common 负责解析/浏览器安全边界，browser 负责登录/因素/同意，logout 负责退出及跨站 relay，oauth 负责标准协议，account 负责本人账户管理。路径、operationId 与 OpenAPI 合同保持一致。

2026-10-27 新增[员工开户与一次性激活](employee-onboarding.md)。`api/native/activation.py` 提供独立 issuer 页面；`NativeActivation` 消费管理员签发的单次证明，由本人设密并完成 TOTP 后原子激活待入职事实。`native_account_activation` 保存哈希／加密秘密及明确来源版本，`native_account` 增加 pending；普通 OAuth 密码流程仍要求 active，不能凭空种子退回密码引导。完成激活不签发 code、token 或 SSO；原有登录与任职授权仍分别执行。恢复码仅在首次完成响应展示，原浏览器在五分钟绝对窗口内只能恢复安全状态。

`native_auth/protocol.py` 定义协议值对象和错误；`current_session.py` 读取当前账号、客户端、RP/浏览器会话事实，不自行取得或释放锁。identity、credentials、OAuth 和会话管理在原有 trust→principal→组织→account 顺序内复用验证，不再为了读取当前会话而导入 OAuth 服务。共享令牌配置名为 `AccessTokenProfile`，`KeycloakProfile` 保留兼容别名。

UserInfo 的 profile 投影直接在一次已验证的账户事务内读取，避免重复取得组织 guard；数据库等待之后仍检查 token/session 截止。OAuth client 配置、原生安装、组织 MFA 和授权管理复用 `app.control` 的公共控制操作协议，原有日志摘要、幂等身份与未知结果恢复保持一致。

GitHub/GitLab 各自的独立 Chromium 作业验收真实登录/恢复、SSO/同意、form_post 和跨站退出，依赖缺失或测试跳过视为失败。运行方法和不可上传的认证临时文件见[交付冻结与浏览器门禁](../deployment/dev/release-freeze.md)。这轮整理不新增 OAuth 协议承诺、Passkey 或管理 HTTP API。
