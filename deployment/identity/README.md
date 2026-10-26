# 本地真实 OIDC 协议验收环境

> 现行人员认证方向见[原生认证运行手册](native-authentication.md)。下文保留 Keycloak 测试 fixture 的历史部署与兼容说明；native-only 组织策略启用后，其旧 human profile 不再获得人员入口准入。

2026-10-26 常驻 `stage-development` 已保留原人员 subject/密码/issuer/client 并启用密码＋TOTP，Stage 组织 policy 已设 required。下文准备脚本描述原始环境构建；新增 MFA 的受控升级及回退另见[MFA 升级方案](MFA-standing-upgrade.md)和[实际验收记录](../../docs/reviews/p1b4-assignments-mfa-2026-10-26.md)。原协议 smoke 已支持真实 OTP 页，测试种子只在私有 fixture 文件中，不是 Stage 数据。

这套 fixture 使用 **Keycloak 26.4.7 + OpenJDK 21**，与 Stage 的 PostgreSQL/Redis 分开。Keycloak 的本地 H2 数据仅保存可丢弃的测试账号；它不是 Stage 主库，也不表示采用 H2 或该版本作为生产 IdP 方案。所有账号都是虚构的本机测试身份，不授予真实企业权限。

服务名 `orchestra-stage-idp-dev.service`，独立非登录用户 `orchidp`，HTTP 仅监听 `127.0.0.1:18080`。`--cache=local` 禁止启动集群监听，管理接口也绑定 loopback。安装目录 `/opt/orchestra-keycloak`；只有其中的 `data` 可由服务写入。unit 的环境文件 `/etc/orchestra-stage-idp-dev.env` 是 root:root 0600。该 fixture 不通过公网反向代理暴露。

## 固定协议合同

- realm `stage-development`；精确 issuer `http://127.0.0.1:18080/realms/stage-development`，API audience `stage-api`。
- `maestro-stage-dev` 是 public client，只启用授权码流程，强制 PKCE S256；禁用 implicit、password grant 和 service account。精确回调 `http://127.0.0.1:18765/callback`。
- `stage-worker-dev` 是 confidential client，只启用 service account；禁用授权码、implicit 和 password grant。本机随机 secret 仅用于协议验收；不据此决定生产工作负载凭据方式。
- 两类 client 都由受控 mapper 加入 access-token audience，并加入互斥的签名 claim `stage_credential_kind=human_oidc` 或 `workload_oauth2`。Stage 同时验证 exact client 和该 claim，避免从 sub/email/用户名猜主体类别。
- Stage 仅接受这个已登记的 Keycloak profile：RS256、header `typ=JWT`、payload `typ=Bearer`、精确 issuer、包含 Stage 的 audience、有效时间和相应 client/kind。它不是任意 JWT/OIDC 发行方的通用兼容声明。
- 认证强度只记录实际验证到的 `auth_time`、`acr`、`amr`；缺失保留未知，不能把 `iat` 当作登录时间或把任意 acr 当作 MFA。

`KeycloakAuthenticator` 在数据库事务之外完成网络及密码学验证。它的 `VerifiedCredential` 尚未包含数据库主体、成员、信任版本或公司准入；Stage 身份服务读取当前记录后才可产生公司内身份投影。profile、映射、workload、主体或公司被撤销后，旧验证结果不能绕过当前数据库检查。

JWKS 地址只能来自受控 profile；拒绝 token 的 `jku/x5u/jwk/crit`，禁止 HTTP 重定向和环境代理。请求有 3 秒 socket timeout、5 秒总 deadline、128 KiB 响应/32 key 上限。缓存默认 60 秒；未知 kid 最快每 5 秒刷新一次。已到期缓存遇网络故障返回 unavailable，不延长旧 key 寿命。IdP 侧远程撤销不会神奇地通知所有 JWT 验证器；紧急本地撤销由 Stage trust/profile 状态及版本控制。缓存仍有效的已知 key 可以在短暂网络故障时使用。

## 重建本机 fixture

安装 OpenJDK 21，然后从 [Keycloak 官方 release](https://github.com/keycloak/keycloak/releases/tag/26.4.7) 下载 `keycloak-26.4.7.tar.gz`。本轮下载的 SHA256 与 GitHub release asset digest 一致：

```text
aca60ab72ae4d67adc6b8fe7fca5e08d16a6bb5e4a4f2ebb54050e3f5503fd2b
```

将发行包解到 `/opt/orchestra-keycloak`，建立非登录系统用户 `orchidp`。在 Stage 仓库内运行：

```bash
uv run python scripts/identity_idp_prepare.py --state-dir /root/.local/state/orchestra-stage/identity
/opt/orchestra-keycloak/bin/kc.sh build --db=dev-file
/opt/orchestra-keycloak/bin/kc.sh import --optimized --file /root/.local/state/orchestra-stage/identity/realm-import.json --override false
chown -R orchidp:orchidp /opt/orchestra-keycloak/data
install -m 600 /root/.local/state/orchestra-stage/identity/keycloak.env /etc/orchestra-stage-idp-dev.env
install -m 644 deployment/identity/orchestra-stage-idp-dev.service /etc/systemd/system/orchestra-stage-idp-dev.service
systemctl daemon-reload
systemctl enable --now orchestra-stage-idp-dev.service
```

prepare 要求私有 0700 目录，并通过排他创建写入 root 0600 文件；已有配置不会被覆盖。生成的 `credentials.json`、`realm-import.json` 和 `keycloak.env` 均包含测试凭据，保存在仓库外，不能粘贴到终端或提交。已导入的 realm 使用 `--override false`，避免重运行重置账号。初始化 Stage 是另一个明确的受控步骤，prepare/import 不写 Stage 数据库。

## 可重复协议冒烟

等待发现文档可用后执行：

```bash
uv run python scripts/identity_idp_smoke.py \
  --credentials /root/.local/state/orchestra-stage/identity/credentials.json \
  --metadata /root/.local/state/orchestra-stage/identity/metadata.json
```

本地测试客户端实际访问 Keycloak 登录表单、提交测试账号、接收授权码并交换 token；校验 state、PKCE、ID token 签名/audience/nonce。它没有使用 password grant，也没有伪造 access token。客户端不启动回调监听，在精确登记的 loopback redirect 处取回 code，不跟随其他跳转。Keycloak 对 loopback 设置 Secure cookie，而 HTTPX 不采用浏览器的 loopback 例外；脚本只为已核验的 `127.0.0.1` fixture cookie 做等价适配，不可复用到远端登录或产品客户端。

成功输出只有检查名称；token、secret、密码留在进程内。`metadata.json` 仅包含已验证的 subject/client/profile 配置，可供 Stage 的显式初始化使用。另验证授权码重放、错误或缺失 PKCE、ID token 作为 API token、service 作为 human 和 password grant 均被拒绝。缺失 PKCE 按 OAuth 合同返回精确回调的 invalid_request，并验证 state 与不含授权码。

Stage 已明确登记对应两主体及测试组织根成员后，追加：

```bash
uv run python scripts/identity_idp_smoke.py \
  --credentials /root/.local/state/orchestra-stage/identity/credentials.json \
  --stage-url http://127.0.0.1:8080 --organization-ref stage-development
```

`--organization-ref` 仅断言响应中的部署组织，不加入 HTTP query，也不选择组织。

这一步使用新交换的真实 token 调用受保护的 `/api/v1/identity/me`，验证 human/service 各自的本组织根投影。还验证缺失 Bearer、伪造身份头、ID token、service token 冒用 human profile 返回 401，尝试通过 query 选择组织返回 422，所有错误响应 no-store 且不含凭据。历史首条切片共 15 项；更新后的检查数量与结果见[本轮记录](../../docs/private-deployment-api-v1.md)。普通 pytest 使用生成的测试 RSA key 和 MockTransport 做边界验证，不依赖该系统服务：

```bash
uv run pytest -q tests/identity/test_authentication.py
```

本轮验证的是服务器协议和 Stage 入口；Maestro/Virtuoso 的实际浏览器、Desktop 系统浏览器、产品重定向注册及 token 保管仍需各自客户端集成验收。
