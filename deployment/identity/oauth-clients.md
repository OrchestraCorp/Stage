# OAuth 客户端管理

状态：2026-10-26，本轮新增的受控客户端管理。服务端协议能力与范围见[原生认证设计](../../docs/native-authentication-design.md)。本页只管理 OAuth 应用，不创建人员、任职、业务权限或独立 Agent 身份。

`native_oauth_admin.py` 目前沿用现有 **dev/test 本机 OS 管理员**边界，要求显式 `STAGE_DATABASE_URL`，并校验数据库环境及固定组织根。它不是公开动态注册 API，也不代表生产管理平台已经验收。所有计划文件与秘密文件必须由当前 OS 用户持有、为普通文件且仅该用户可读写；不要将它们放入仓库。

## 注册与更新

使用 `uv run python scripts/native_oauth_admin.py --environment dev configure --plan /private/client.json --idempotency-key register-knowledge-v1`。首次注册 `expected_version` 为 `0`；更新、停用、重新启用时填写 `inspect` 返回的当前版本，并使用新的幂等键。计划必须完整列出新的配置，避免遗漏字段退回默认值。

```json
{
  "environment": "dev",
  "organization_ref": "stage-development",
  "client_id": "knowledge-web",
  "expected_version": 0,
  "display_name": "知识应用",
  "application_type": "web",
  "token_endpoint_auth_method": "client_secret_basic",
  "redirect_uris": ["https://knowledge.example/callback"],
  "post_logout_redirect_uris": ["https://knowledge.example/signed-out"],
  "allowed_scopes": ["openid", "profile", "offline_access"],
  "require_consent": true,
  "resource_audience": "stage-userinfo",
  "enabled": true
}
```

以上为合成示例，不是已注册的真实客户端。`application_type` 可为 `native` 或 `web`；公开客户端使用 `token_endpoint_auth_method: "none"`，服务端保密客户端使用 `client_secret_basic`。注册后认证方式固定，变更类型应注册新客户端。保密客户端同样必须使用授权码与 S256 PKCE。

新注册默认要求同意，仅向 `stage-userinfo` 签发访问令牌。允许范围只可包含 `openid`、`profile`、`offline_access`，其中 `openid` 必须存在；配置允许某范围不等于每次登录自动授予该范围。`require_consent: false` 是管理员对可信应用的显式配置，不能解除协议对离线授权的同意要求。

回调必须精确登记，不支持通配符。允许 HTTPS，或带显式端口的 `127.0.0.1` / `[::1]` HTTP 回调；URI 的 origin 要采用浏览器规范拼写。拒绝 fragment、用户信息、控制字符，以及与协议响应冲突的 `code`、`state`、`error`、`error_description`、`error_uri`、`iss` 查询参数。登出回调单独登记。

认证服务必须使用专用 hostname，应用的授权和登出回调不得使用 issuer 的同一 hostname。换端口、路径或查询参数不能隔离 Cookie；即使 `__Host-`、Secure、HttpOnly 都启用，同一 hostname 的另一个 HTTPS 端口仍会收到认证 Cookie。此边界在注册和运行时都检查，也约束旧登记及待完成请求。只有 dev/test 环境且 issuer 为 loopback 时保留本地联调例外；生产 HTTPS loopback 也不豁免。当前没有同 hostname 产品应用的例外。

## 保密客户端秘密与结果恢复

首次创建 `client_secret_basic` 客户端时，额外提供 `--secret-output /private/knowledge-secret.json`。工具先生成 32 字节随机秘密，创建新的 `0600` 文件并同步文件和目录，再执行数据库事务。目的文件已存在、不是可安全创建的文件或同步失败时，不会变更数据库。标准输出、审计和结果查询均不返回秘密；数据库仅保存其 SHA-256 摘要。此摘要方案仅用于工具生成的高熵随机秘密，不用于人的密码。

若数据库返回 `result_unknown`，保留私密文件，按原 OS 身份和幂等键查询：

```bash
uv run python scripts/native_oauth_admin.py --environment dev result \
  --operation-type configure.native-oauth-client --idempotency-key register-knowledge-v1
```

需要重试同一操作时，使用**相同计划、相同幂等键**以及 `--secret-input /private/knowledge-secret.json`。工具检查私密文件中的客户端、操作类型、幂等键与计划摘要；不重新生成秘密。数据库和文件之间没有跨系统原子提交：在数据库明确拒绝时，文件可能只是尚未启用的候选秘密；在结果未知时，不要删除文件或凭空更换幂等键。

轮换计划仅包含 `environment`、`organization_ref`、`client_id`、`expected_version`，通过 `rotate-secret --plan ... --idempotency-key ... --secret-output ...` 提交。原秘密立即失效，该客户端的现有应用会话全部撤销，其他客户端会话不受影响。轮换的结果查询类型为 `rotate.native-oauth-client-secret`。私密文件丢失时无法从服务器取回明文；确认当前版本后执行新的显式轮换。

## 显式准入 Stage API

OAuth 注册不自动扩展 Stage 的认证信任名单。应用确实需要调用 Stage API 时，先在完整 `configure` 计划中显式设置 `resource_audience: "stage-api"`；再通过 `admit` 提交独立计划：

```json
{
  "environment": "dev",
  "organization_ref": "stage-development",
  "client_id": "knowledge-web",
  "expected_client_version": 1,
  "profile_ref": "knowledge-web-native",
  "binding_ref": "knowledge-web-native-binding",
  "expected_policy_version": 3
}
```

版本仅为格式示例，必须读取当前配置后填写。执行 `uv run python scripts/native_oauth_admin.py --environment dev admit --plan /private/admission.json --idempotency-key admit-knowledge-v1`。

这一步创建独立、不可就地修改的认证 profile（版本 `1`）及来源 binding，复用已有原生 provider、人员 subject namespace 和外部身份映射。它不会追加到已发布的 `stage-native-human@1` 内容中。若组织 MFA 策略为 required，同一事务会在精确版本校验后追加新 profile 的 `pwd`＋`otp` 规则，保留原规则和认证新鲜度，递增策略版本、组织 epoch，并写入完整审计。若策略原本 disabled，此操作不擅自启用或重写组织策略。单个策略最多 32 个 profile 规则，达到上限时整体拒绝。

调用 Stage 身份、任职或业务 API 时，仍需选择这次明确准入的 `profile_ref` / `profile_version`，并通过当前人员、成员和业务授权检查。此操作不授予任何角色、任职或资源权限。

## 生命周期与并发

`inspect --client-id knowledge-web` 返回安全元数据和当前版本。`configure` 通过 `enabled: false` 停用客户端；重新启用仍是一次带预期版本的新更新。每次配置更新或秘密轮换都会递增 client version，并在同一事务撤销其现有应用会话。旧授权请求与旧会话携带先前 client version，不能在重新启用后复活；当前浏览器登录可继续供其他应用使用。

管理员写入按 native TrustState 排他锁 → 组织 guard → 客户端/会话的顺序执行，与业务读写持有的 trust 共享锁协调。先取得锁的业务事务可以完成；管理员变更提交后，新身份读取和业务提交必须看到新版本或撤销状态。组织 guard 仍沿用现有串行策略，容量与多节点负载需另外验收。

所有管理操作使用请求摘要、幂等键、预期版本和结果 journal。审计失败会回滚配置、策略、epoch 和会话撤销；数据库提交回执丢失按未知结果处理，查询原操作，不重复生成秘密。

## RP 发起的浏览器退出

应用可打开 `/oauth/logout`，携带 `id_token_hint`，或明确的 `client_id`；回跳使用已登记的 `post_logout_redirect_uri`，可附带调用方 `state`。过期但签名有效的 ID token 仅用来识别应用和关联账户，不是撤销凭据。GET 只创建短期确认页，POST 必须持该页的 cookie、CSRF 证明及原浏览器 SSO cookie。

确认后，Stage 在同一事务中关闭该 SSO 浏览器登录及从该浏览器产生的所有应用会话，然后回到精确登记的地址并回传 state。它不退出同一人员的其他浏览器。取消只消费确认请求，不撤销登录，也不回跳。客户端登记被停用或回跳地址在确认前被移除时，提交会拒绝，不向旧地址跳转。

没有 SSO cookie 时，可完成已登记应用的确认和回跳，但仅凭 ID token 不会撤销任何应用会话。原生客户端仍应显式调用 token 撤销接口清理自身 refresh family；浏览器退出不代替 RP 清理本地会话。跨应用 back-channel/front-channel logout 通知不在本轮实现范围。
