# 隔离登录 MFA 协议验收

用户选择的首个登录因素组合是密码＋TOTP，由 IdP 验证。这里的 fixture 验证 Keycloak 实际执行这两个步骤并签发相应证据；Stage 不保存密码或 TOTP 种子，不实现登录 OTP 校验器。它不等于交易批准或 Passkey ceremony，也不能替代 Maestro 的系统浏览器验收。

本机独立实例使用 Keycloak 26.4.7、loopback `18082`、管理端口 `19002`，realm 为 `stage-mfa-development`。它使用专属 H2 测试数据，与既有 `18080` IdP 和 Stage PostgreSQL 分离。H2 只是可丢弃 IdP fixture 的存储方式，不是 Stage 数据库选型。

2026-10-26 验收后已停止本轮临时 `18082` 测试进程并保留私有配置；复跑 opt-in MFA 集成前按下方步骤启动。常驻 `18080` IdP 与 `8080` Stage 持续运行，不依赖此临时进程。

## 真实强度来源

- `stage-mfa-desktop` 是授权码＋PKCE S256 public client，最低 ACR 是 `mfa`。implicit、password grant 和 service accounts 关闭。
- `stage-weak-negative-fixture` 只用于弱凭据拒绝测试，最低 ACR 是 `pwd`；不得把这个客户端登记为正式 MFA 的替代路径。
- browser flow 的 LoA 1 条件子流执行密码，LoA 2 条件子流执行 OTP。`conditional-level-of-authentication` 在子流成功后记录对应 LoA；最大复用时间设为 0，fixture 每次登录重新执行因素。
- `oidc-acr-mapper` 将实际达到的 LoA 映射为 `pwd=1` 或 `mfa=2`。`oidc-amr-mapper` 读取实际完成的 execution，分别产生 `pwd`、`otp`。执行引用有效期明确设置为 600 秒。
- `basic` client scope 保留真实 `sub` 和 `auth_time`。刷新不能被解释为新登录，也不能用 token 的 `iat` 代替 `auth_time`。
- 只有主体类别 `stage_credential_kind=human_oidc` 使用固定 mapper；ACR、AMR 和认证时间均不能使用固定 claim mapper。
- OTP fixture 使用显式预注册的虚构测试账号。种子仅存在仓库外私有文件及进程内存，OTP 重用被禁用。它不覆盖实际用户绑定、换机或账户恢复体验。

实现依据为 Keycloak 26.4.7 官方源代码的 [ConditionalLoaAuthenticator](https://github.com/keycloak/keycloak/blob/26.4.7/services/src/main/java/org/keycloak/authentication/authenticators/conditional/ConditionalLoaAuthenticator.java)、[AcrProtocolMapper](https://github.com/keycloak/keycloak/blob/26.4.7/services/src/main/java/org/keycloak/protocol/oidc/mappers/AcrProtocolMapper.java) 和 [AmrProtocolMapper](https://github.com/keycloak/keycloak/blob/26.4.7/services/src/main/java/org/keycloak/protocol/oidc/mappers/AmrProtocolMapper.java)。

## 重建与运行

先准备独立的优化 Keycloak 发行目录；可复制既有发行目录的 `bin`、`conf`、`lib` 等静态内容，但**不要复制 `data`**。本机目录是 `/root/.local/state/orchestra-stage/identity-mfa/keycloak`。父 state 目录必须是 0700，生成的凭据文件是 0600。准备脚本排他创建文件，不覆盖已有账号或种子：

```bash
uv run python -m scripts.identity_mfa_prepare \
  --state-dir /root/.local/state/orchestra-stage/identity-mfa

/root/.local/state/orchestra-stage/identity-mfa/keycloak/bin/kc.sh import \
  --optimized --override false \
  --file /root/.local/state/orchestra-stage/identity-mfa/realm-import.json

JAVA_OPTS_APPEND='-Xms128m -Xmx512m' \
  /root/.local/state/orchestra-stage/identity-mfa/keycloak/bin/kc.sh start \
  --optimized --cache=local --http-enabled=true \
  --http-host=127.0.0.1 --http-port=18082 \
  --hostname=http://127.0.0.1:18082 \
  --http-management-host=127.0.0.1 --http-management-port=19002
```

启动后，在另一终端运行：

```bash
uv run python -m scripts.identity_mfa_smoke \
  --credentials /root/.local/state/orchestra-stage/identity-mfa/credentials.json
```

脚本提交真实密码和 OTP 表单，校验精确 loopback callback、state、PKCE、签名 ID token 和 nonce，并用 Stage `KeycloakAuthenticator` 验证真实 access token。输出只有 issuer 和检查名称。表单地址限定配置的 IdP origin，不跟随重定向，不启动回调监听。HTTPX loopback Secure cookie 适配仅适用于本机 fixture。

检查覆盖弱登录强度、弱 session／低 ACR 请求不能跳过第二因素、未完成／错误 OTP 无授权码、成功 MFA 的 ACR/AMR/认证时间、刷新不得提升弱认证或重置登录时间、带 OTP 的 password grant 仍被拒绝。相邻测试若已消耗同一 30 秒 TOTP，helper 最多等待下一个周期重试一次；不会启用 OTP 重用。

`read_config`、`profile`、`login_tokens`、`start_login`、`submit_password` 和 `submit_otp` 可以供显式启用的 Stage 真实 HTTP 测试复用。这些 helper 不初始化 Stage，也不授予组织成员、任职或权限。Stage 的组织级 MFA 策略、弱 profile 绕过阻断和提交前重验必须另用真实 Stage 数据库验证。

准备此独立 fixture 时，旧常驻 IdP 的 bootstrap admin 凭据返回 401，因此当时没有变更旧 realm。后续已通过单独的受控备份、临时管理入口和有限升级启用常驻 MFA，见[实际记录](../../docs/reviews/p1b4-assignments-mfa-2026-10-26.md)。独立 fixture 本身仍只证明隔离协议路径，不能单凭它推断其它部署策略已启用；正式接入须按部署方流程验收。
