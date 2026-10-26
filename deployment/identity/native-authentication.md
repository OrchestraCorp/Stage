# Stage 原生认证开发运维手册

> 多应用 OAuth/OIDC 已在首版基础上扩展：当前 head 为 `c6f3a19d820b`、37 张表。新客户端登记/业务准入见[管理手册](oauth-clients.md)，现行能力见[完整性矩阵](../../docs/oauth-functional-coverage.md)。下文原生账号安装与因素恢复步骤继续适用。

本手册对应[自研最小认证设计](../../docs/native-authentication-design.md)。当前实现用于预登记的第一方 public client，提供密码＋TOTP、授权码＋PKCE S256、原生 access token、刷新、撤销与最小 OIDC。它不代表通用 OAuth/OIDC 兼容认证、第三方动态接入或正式生产 IAM 管理平台已经验收。

## 1. 部署边界与数据

认证服务在 Stage 进程内，通过 `native_auth_plugin` 装配。PostgreSQL 保存凭据与会话事实；Redis 不承担一次性凭据消费或撤销的权威。首次原生迁移为 `e8a1c6d9f240`，前置为 `9c2e7a4b6d10`；安全审计后当轮 head 为 `b7f0d3a9216c`，新增 `totp_bootstrap_allowed` 区分首次引导与受保护重绑。新增八张表：

| 表 | 用途 |
|---|---|
| `native_account` | 精确关联已有 HUMAN Principal、Argon2id 密码摘要、加密 TOTP、凭据版本、恢复码摘要 |
| `native_client` | 显式登记的 public client 和精确回调 |
| `native_login_request` | 短期浏览器认证事务、因素进度、客户端/PKCE/浏览器绑定 |
| `native_session` | 原始认证时间、认证强度、会话期限、撤销状态 |
| `native_authorization_code` | 短期授权码摘要、回调和 PKCE 绑定、消费状态 |
| `native_refresh_token` | 不透明刷新凭据摘要、会话族和消费状态 |
| `native_auth_event` | 不含凭据明文的认证与恢复审计 |
| `native_login_throttle` | 持久登录尝试预算和冷却状态 |

现有 Principal、成员经历、任职、角色、业务对象和审计引用保持不变。安装器不创建公司成员，不按邮箱/用户名猜测人员关系，不授予业务权限。Keycloak 对照身份与数据可以保留；是否继续允许其进入 Stage，取决于显式组织认证政策。

迁移前备份数据库，执行 `uv run alembic upgrade head` 并检查 `uv run alembic current`。迁移 downgrade 对已经产生的原生认证事实拒绝删除，不能把回滚应用代码等同于清空认证表。

## 2. 创建部署私钥

签名私钥和 TOTP 加密主密钥必须在数据库外保存，不能进入 Git、环境输出、SQL 日志或普通工单。开发安装示例：

```bash
install -d -m 0700 /etc/orchestra-stage/native-auth
uv run python scripts/native_auth_admin.py --environment dev generate-keys \
  --key-file /etc/orchestra-stage/native-auth/signing.json \
  --encryption-key-file /etc/orchestra-stage/native-auth/totp.key
```

工具创建 RSA 3072 签名密钥和 Fernet 加密密钥，文件权限 0600，拒绝覆盖已有文件。输出只包含路径和公开 key ID。若服务使用独立系统账号，部署者应配置专用服务组、可遍历的私有目录及仅该服务组可读的 0640 key 文件；运行时不接受其他组/其他用户可写或所有人可读的文件。

安装计划、恢复计划必须属于执行操作者、权限 0600、为普通文件且不能是符号链接；这项限制比服务读取 key 的权限更严格。

## 3. 显式安装已有人员的本地账号

首先确认目标部署的既有组织根、HUMAN Principal、当前 membership 引用。用户名只是本地登录名，不是关联证据。将如下结构写入仓库外 0600 文件，替换引用与密码占位符，禁止把真实计划粘贴到终端输出：

```json
{
  "environment": "dev",
  "organization_ref": "organization:example",
  "issuer": "http://127.0.0.1:8080",
  "client_id": "maestro-native-dev",
  "redirect_uris": ["http://127.0.0.1:18765/callback"],
  "account_ref": "account:example-person",
  "principal_ref": "person:existing-person",
  "membership_ref": "membership:existing-current-tenure",
  "external_identity_ref": "identity:native-example-person",
  "username": "example-person",
  "password": "REPLACE_WITH_A_PRIVATE_UNIQUE_PASSWORD"
}
```

发行地址是固定配置，开发仅允许 loopback HTTP；正式地址要求 HTTPS，不接受请求 Host/Forwarded 或 token 自报 issuer。回调要求精确登记，不支持通配、fragment、带用户名密码的 URL；桌面 loopback 使用明确 IP 和端口。

通过明确的 `STAGE_ENV`、`STAGE_DATABASE_URL` 指向正确开发库，再执行：

```bash
uv run python scripts/native_auth_admin.py --environment dev install \
  --plan /private/operator/native-install.json \
  --encryption-key-file /etc/orchestra-stage/native-auth/totp.key \
  --idempotency-key native-account-install-001
```

安装器验证数据库实际 bootstrap 环境、现有人及当前成员经历。它原子创建/核验 `trust:native-auth`、`provider:native-auth`、`stage-native-human@1`、`binding:native-auth`、client、account 和精确 ExternalIdentity 映射，保存幂等 journal。初始 TOTP 未绑定，必须在浏览器中证明密码并实际输入动态码后才签发正常 MFA token。

当前安装器固定一个 profile 和一个 client，允许后续为同一配置安装多个不同人员账号；它不会静默扩大 client allowlist、修改回调、重建旧账号或复活已撤销来源。新增另一 client/profile 属于后续显式配置变更能力。

相同操作者、幂等键、输入会返回原安装记录；同键不同密码/映射会冲突。密码参与使用部署私密 key 的 HMAC 承诺，不把包含密码的快速 SHA 留在数据库中。安装记录是历史结果，不能用它判断人员当前仍有效。

## 4. 启用服务和收紧认证来源

Stage 配置项如下，key 路径只是文件位置，不是 key 内容：

```dotenv
STAGE_NATIVE_AUTH_ENABLED=true
STAGE_NATIVE_AUTH_ISSUER=http://127.0.0.1:8080
STAGE_NATIVE_AUTH_ORGANIZATION_REF=organization:example
STAGE_NATIVE_AUTH_KEY_FILE=/etc/orchestra-stage/native-auth/signing.json
STAGE_NATIVE_AUTH_ENCRYPTION_KEY_FILE=/etc/orchestra-stage/native-auth/totp.key
STAGE_NATIVE_AUTH_REQUEST_TIMEOUT_SECONDS=15
```

完成迁移、账号安装和文件权限配置后，按本机既有部署方式停止接入、排空并重启 Stage。确认 `/health`、`/ready` 正常，以及 `/.well-known/openid-configuration` 的 issuer、端点和 `/oauth/jwks` 公钥符合预期。

安装不会自动改组织 MFA policy。通过现有 `scripts/identity_admin.py configure-human-authentication` 版本化操作，显式设置/更新原生规则：`profile_ref=stage-native-human`、`profile_version=1`、`required_acr=urn:orchestra:acr:pwd-totp`、`required_amr=[pwd,otp]`。需要保留 15 分钟认证新鲜度时设置 `max_auth_age_seconds=900`。迁移验证期间的旧/新 profile allowlist 必须明确；验证结束后再收紧为目标 allowlist。没有在 allowlist 中的旧强认证 profile 同样被拒绝。

不能通过关闭组织 MFA、修改旧 profile 语义或自动关联邮箱来绕过迁移失败。Keycloak 的旧 access token 是否仍可用要实际验证，不能只看新登录页已可使用。

## 5. 客户端合同与测试

| 入口 | 当前合同 |
|---|---|
| `GET /.well-known/openid-configuration` | 有限 OIDC 发现元数据 |
| `GET /oauth/jwks` | 当前及保留的验签公钥 |
| `GET /oauth/authorize` | `response_type=code`、`scope=openid`、非空 state/nonce、PKCE S256、精确 client/redirect |
| `POST /oauth/login` | 浏览器秘密 cookie＋CSRF、密码与第二因素分阶段验证 |
| `POST /oauth/token` | 授权码交换或 refresh，不支持 password/implicit/client_credentials grant |
| `POST /oauth/revoke` | public client 持有原 token 的证明，撤销整个对应会话 |
| `GET /oauth/userinfo` | 当前有效 access token，只返回 `sub` |
| `POST /api/v1/account/password` | 近期强认证、原密码、凭据与会话原子重验 |
| `POST /api/v1/account/totp/reset` | 近期强认证、密码和当前 OTP；200 返回仅一次的重绑码，撤销旧会话 |

默认 access token 5 分钟、授权码 60 秒、浏览器认证事务 5 分钟、会话最长 8 小时。刷新受原会话终止时间限制，保留原 `auth_time`/ACR/AMR，不延长组织 MFA 新鲜度。账户敏感操作要求原认证在 5 分钟以内。

客户端必须单飞刷新：同一 refresh 并发重用只有一个交换可能成功，重用检测会撤销整条会话族，因此该成功响应中的新 token 也可能随后失效。授权码重用同样拒绝并撤销原会话。网络结果未知时不得无条件循环重试旧 code/refresh；重新开始登录是可用的恢复路径。

授权码按协议进入已登记回调的 query，客户端校验 state、交换 code 并清理回调地址。授权码、token、密码、OTP 和恢复码不得进入日志、审计、错误正文；全部认证响应采用 no-store；登录 HTML 使用 same-origin Referrer-Policy，JSON/303 使用 no-referrer。Maestro 应保存自己的 verifier/nonce，验证 ID token 的签名、issuer、audience、nonce，API 使用 access token。

可重复测试入口：

```bash
uv run pytest -q tests/native_auth
uv run python scripts/native_auth_smoke.py --credentials /private/operator/native-fixture.json --enroll
```

smoke 的 `--enroll` 只用于虚构开发 fixture，完成真实绑定并把测试种子/恢复材料写回私有文件；正常人员在认证器中持有因素，服务操作者不应收集个人 TOTP 种子。具体测试结果以本轮 review 为准。PostgreSQL 单次消费和撤销竞争必须在真实库验收，SQLite 快测不能替代它们；正式 Maestro 浏览器/系统凭据存储另行验收。

## 6. 恢复、停用和未知结果

用户持密码＋一次性恢复码可以进入重新绑定；只消费所用码并原子撤销旧会话，未消费的码可在中断后重新发起，成功绑定新 TOTP 后才更换整批恢复码并取得正常 token。恢复、重置后的账号不允许密码单独绑定。自助 reset 的 200 响应仅一次返回 enrollment_code；先保存，再在新的密码登录中通过恢复表单使用它。该响应丢失时不能用旧令牌重新提取，需操作者恢复。

无恢复码时，当前只支持明确的本机 dev/test 操作者恢复。创建 0600 恢复计划：

```json
{
  "account_ref": "account:example-person",
  "expected_version": 1,
  "new_password": "REPLACE_WITH_A_PRIVATE_UNIQUE_PASSWORD",
  "reset_totp": true
}
```

```bash
uv run python scripts/native_auth_admin.py --environment dev reset \
  --plan /private/operator/native-recovery.json \
  --enrollment-output /private/operator/new-enrollment.json \
  --organization-ref organization:example \
  --encryption-key-file /etc/orchestra-stage/native-auth/totp.key

uv run python scripts/native_auth_admin.py --environment dev disable \
  --account-ref account:example-person --expected-version 2 \
  --organization-ref organization:example \
  --encryption-key-file /etc/orchestra-stage/native-auth/totp.key
```

因素重置前先验证新的输出文件可创建；重绑码只写入 0600 文件，控制台不输出其内容。向对应人员安全交付该文件中的码，人员仍需正确密码才能使用它。不得覆盖旧输出文件；提交或文件落盘结果未知时先 inspect，不盲重试。

修改/恢复/停用递增凭据版本并撤销旧会话，记录本机操作者与认证事件。现有 API token 随当前账号/会话重验失效，不必等待 JWT 自身过期。`reset` 不把已禁用账号自动恢复为 active；当前没有公开的重新启用管理入口。

安装返回 `result_unknown` 时使用原操作者和幂等键查询：

```bash
uv run python scripts/native_auth_admin.py --environment dev result --idempotency-key native-account-install-001
uv run python scripts/native_auth_admin.py --environment dev inspect --account-ref account:example-person
```

安装有原子 journal；恢复/停用以预期凭据版本和认证事件核对，不能伪称通用幂等回放。恢复/停用结果未知时先 inspect，确认是否已发生，再决定后续操作；禁止只增加 expected_version 然后盲重试。HTTP 单次凭据消费的未知结果也不能等同于已回滚。

## 7. 签名密钥换代

```bash
uv run python scripts/native_auth_admin.py --environment dev prepare-key-rotation \
  --key-file /private/operator/current-signing.json \
  --output /private/operator/next-signing.json
```

此命令只创建新候选 ring，不修改正在运行的配置：生成一个新的 active private key，将旧 active 转为 public，保留已有 public keys。输入使用操作者私有 0600 文件，输出拒绝覆盖；最多保留八个 key，达到上限时要求另行明确设计退休，不能静默丢弃旧公钥。

当前采用整实例部署切换：停止新接入 → 排空在途请求/事务 → 停止 Stage → 部署候选文件和服务读取权限/配置路径 → 重启 → 检查 readiness、JWKS、新签名及旧 token 验证。保留旧公钥至少覆盖所有仍需验证的已发行凭据；回退应用或旧 ring 时，若丢失新公钥，会使新签凭据不可验证，不能把这种行为描述为无损回退。

运行时不会监听磁盘自动热加载 key。当前没有在线 key 撤销管理 API、自动 retirement 或多节点 key 滚动协调。泄露私钥的事件响应需要显式撤销会话/来源并制定 key 移除计划，不能仅靠“生成另一个 active key”宣称旧凭据已撤销。

## 8. 备份和现阶段限制

数据库备份必须与签名 ring、TOTP 加密 key、发行配置一起进入受控恢复方案，key 备份仍独立于数据库备份访问权限。丢失 TOTP 加密 key 会使已绑定种子无法解密；丢失签名配置会阻断新发行和本地验证。恢复旧数据库还可能恢复已经消费的恢复码、refresh 或撤销前会话，不能直接对外重新开放；先按恢复时间点处理会话失效及因素恢复，再验收。

现有实现已提供受控 dev/test 客户端登记与管理、顶层浏览器 SSO，以及离线账号安装和恢复；尚不包含生产人员管理 UI/API、公开动态客户端注册、身份联邦、邮箱找回、跨 RP 通知型退出或官方 OIDC 一致性认证。认证请求/会话/审计的长期保留与受控清理需要纳入部署容量管理；清理不得提前删除仍需检测重用的刷新记录。生产发布还需独立完成运维责任、恢复演练、客户端验收和容量验收。

## 9. 本机验收与可重复冒烟

本开发设备的 issuer 是 `http://127.0.0.1:8080`，已登记 `maestro-native-dev` / `http://127.0.0.1:18765/callback`，profile 为 `stage-native-human@1`。组织 required/v4 仅准入该原生强认证，最长认证年龄 900 秒。旧 Keycloak 保留为参考服务，停止它时原生登录及 Stage 身份链仍验证通过；具体证据见[本轮验收](../../docs/reviews/native-authentication-2026-10-26.md)。这不是可以直接公开使用的生产 issuer；远端 Maestro 联调需要明确的 loopback 转发或重新固定 HTTPS issuer/profile。

私有 fixture 包含 issuer、client_id、redirect_uri、username、password、profile_ref/profile_version、principal_ref；首次 `--enroll` 后写入 TOTP 与恢复码，要求本人拥有的 0600 文件并置于仓库外。HTTP 冒烟不创建任何人员、任职或工作项，结束时撤销所创建的会话：

```sh
uv run python scripts/native_auth_smoke.py --credentials /private/operator/native-fixture.json --enroll
uv run python scripts/native_auth_smoke.py --credentials /private/operator/native-fixture.json
```

可选 Chromium 冒烟使用已经绑定因素的 fixture，需另行安装 Playwright/Chromium；不把浏览器依赖加入生产 Python 服务。`STAGE_PLAYWRIGHT_MODULE` 和 `STAGE_CHROMIUM_EXECUTABLE` 可选择开发设备已安装的路径，`STAGE_SMOKE_PYTHON` 可指定包含 PyOTP 的 Python：

```sh
node scripts/native_auth_browser_smoke.cjs /private/operator/native-fixture.json
```

它会在已登记的 loopback 回调端口开启真实 HTTP 监听，等待新的 TOTP 时间步，以真实页面提交密码/OTP，验证 callback/state、无跨源 Referer、PKCE、同一人员业务身份和退出。不要与同一账号的其他因素测试并行运行。脚本输出只含检查结果，不输出密码、OTP、授权码或 token。

登录 HTML 必须同时兼容安全边界和真实浏览器：使用 `Referrer-Policy: same-origin` 保留表单 Origin，CSP 仅允许本站与该登录请求的已登记 callback origin；协议 JSON/303 使用 `no-referrer` 和严格 CSP。不能通过接受任意 `Origin: null`、通配 callback 或放宽精确 redirect 校验解决浏览器问题。

## 10. 审计后的真实浏览器重绑复测

[深度审计](../../docs/reviews/native-authentication-audit-2026-10-26.md)新增一次性测试库的浏览器替换流程。显式提供已批准可重置的 `TEST_DATABASE_URL`、`STAGE_ALLOW_TEST_DATABASE_RESET=1`、`STAGE_PLAYWRIGHT_MODULE` 和可选 `STAGE_CHROMIUM_EXECUTABLE` 后，运行：

```sh
uv run pytest -q tests/integration/test_native_recovery_browser.py
```

测试会启动完整 Stage 和真实回调监听，通过 HTTP 首次绑定，再用 Chromium 重置因素、验证密码抢绑被拒绝、消费重绑码并实际绑定新 TOTP。专用脚本 `scripts/native_auth_recovery_browser_smoke.cjs` 只接受带隔离测试 purpose 标记的私有 fixture，不能用于真实人员账号；不应直接重置部署数据库或生产因素来模拟攻击。
