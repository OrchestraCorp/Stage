# 既有本机 IdP 的 MFA 升级方案

状态：本机开发升级方案已于 2026-10-26 执行并验证，实际步骤、保留范围与临时管理员清理见[P1b.4 记录](../../docs/reviews/p1b4-assignments-mfa-2026-10-26.md)。以下说明最初按只读核对准备，现保留作为受控复现与恢复操作指南；其他部署不能因本文存在就视为已启用 MFA。

受控执行脚本已准备：[identity_mfa_upgrade.py](../../scripts/identity_mfa_upgrade.py)。REST mock 测试覆盖成功、完成后重复执行不写入、六个提交后丢响应位置的恢复、拒绝非本轮 OTP、拒绝完成后强度配置漂移；这些不能替代对常驻 IdP 的实际执行验收。

范围是既有 `http://127.0.0.1:18080/realms/stage-development`：保留 realm、issuer、人员 subject、人员密码、`maestro-stage-dev` client ID、回调、audience 以及 service client。仅增加真实密码＋TOTP browser flow、人员客户端强度配置、测试人员 OTP 凭据。严禁通过 realm override/import 或删除重建人员完成升级。Stage 组织 MFA 策略是独立受控变更，应在真实 IdP 强登录已验证后启用。

## 恢复短期管理入口

`credentials.json` 中旧 bootstrap admin 对 `master/admin-cli` 的登录返回 `401 invalid_grant / Invalid user credentials`。官方说明启动环境变量只在 master realm 尚不存在时创建初始管理员；旧环境先 offline import、后启动可以造成已有 master 却没有该管理员。当前证据不足以认定管理员从未存在，因此不重设或覆盖该用户名。

Keycloak **26.4.7** 的 [bootstrap-admin 文档](https://github.com/keycloak/keycloak/blob/26.4.7/docs/guides/server/bootstrap-admin-recovery.adoc) 支持为已有 master 独立创建临时管理员。所有访问同一数据库的 Keycloak 节点必须先停止。本机只有一个常驻节点；`18082` fixture 使用另一个 H2，可以继续运行。

操作步骤：

1. 以 root 在仓库外新建 0700 操作目录，生成唯一临时用户名和随机密码，写入 0600 文件。密码只通过子进程环境变量传入，不出现在命令行、终端或 shell history。
2. 停止 `orchestra-stage-idp-dev.service`，确认进程结束及 `18080` 关闭。
3. 在停止状态下复制完整 `/opt/orchestra-keycloak/data` 到私有备份目录；保存文件所有权、权限与 SHA256 清单。备份含身份和签名材料，不可提交或打印内容。
4. 用原数据目录、原优化 build 执行：

   ```text
   /opt/orchestra-keycloak/bin/kc.sh bootstrap-admin user --optimized \
     --username:env STAGE_MFA_OPERATOR_USERNAME \
     --password:env STAGE_MFA_OPERATOR_PASSWORD --no-prompt
   ```

   `STAGE_MFA_OPERATOR_*` 从私有文件加载到子进程环境。可以 root 执行后恢复 `data` 为 `orchidp:orchidp`，或保持原服务身份执行；不能修改发行目录构建配置。不要在环境里输出密码，也不要传 `--password <literal>`。
5. 启动原 systemd 服务，检查发现文档和原人员密码登录。临时管理员登录 master 获得短期管理 token，token 只留内存。
6. 全部修改和验证结束后，删除**此次创建且精确匹配 ID/username 的**临时 master 用户；在删除前保存需要的只读快照和检查结果。删除后检查查询不可再获取该用户，临时密码重新换 token 失败，移除本地临时凭据文件。

临时 operator 文件格式仅为 `admin_username`、`admin_password` 两键，root 0600。备份、bootstrap 和服务恢复由部署操作者执行；升级脚本不会自行停服或恢复管理员：

```bash
uv run python -m scripts.identity_mfa_upgrade \
  --credentials /root/.local/state/orchestra-stage/identity/credentials.json \
  --operator-credentials /root/.local/state/orchestra-stage/identity/operator-mfa.json \
  --state-dir /root/.local/state/orchestra-stage/identity/mfa-upgrade
```

脚本先保存 `credentials-before.json` 与 `owned-upgrade.json`，所有新增对象预先获得稳定 UUID；通过精确 GET 校验已存在对象，再创建缺失对象。相同状态重复执行不产生重复 OTP 或 execution，完成后的配置漂移直接拒绝。最后更新既有私有 credentials 文件中的 `otp_secret` 和 owned state 路径；原密码、issuer、subject 和其他字段保留。脚本读取的管理员 token 只留内存，输出只有 issuer 和检查名称。

删除临时 operator 的 REST 顺序为：`GET /admin/realms/master/users?username=<exact>&exact=true`，断言返回只有此次用户名并记录 ID；`DELETE /admin/realms/master/users/{id}`，必须为 204；再以同一临时用户名/密码交换 token，必须失败。自删除后原 access token 也可能立即被拒绝，不依赖它继续做其他管理操作。不得把 master 中其他用户当作清理对象。

停止窗口中任何失败，都应先恢复原服务可用性；只有确认需要回退时，才在停止状态下从完整备份恢复 H2。不得在运行中的 H2 文件上覆盖或复制回写。

## REST 执行前断言与快照

所有 URL 固定于 `127.0.0.1:18080`，不跟随重定向，不接受 token/profile 请求提供的端点。先读取并断言：

| 读取 | 必须核验 |
|---|---|
| `GET /admin/realms/stage-development` | realm/issuer 对应既有 fixture；OTP 为 TOTP、HmacSHA1、6 位、30 秒，复用关闭；不更改全 realm browserFlow |
| `GET .../clients?clientId=maestro-stage-dev` | 只有一个精确匹配 client；public、授权码开启、PKCE S256、implicit/password/service-account 关闭、回调符合既有配置 |
| `GET .../users/{existing-human-subject}` | ID/username 精确匹配私有 fixture；账号有效 |
| `GET .../users/{subject}/credentials` | 保存现有 password credential ID；已有非本轮 OTP 时停止检查，不覆盖它 |
| `GET .../authentication/flows` | 预定新 flow alias 尚未被其他配置占用 |
| `GET .../clients?clientId=stage-worker-dev` | 保存 service client 表示和 service-account subject，变更后精确比对 |

上述读取结果保存在 0600 快照文件。原 client attributes、mapper 和 browser override 均保留备份。读取 credentials API 会屏蔽 secretData，完整灾难回退依靠停止状态下的 H2 备份。

## 新建独立 flow，最后才绑定客户端

可以复用[隔离 fixture](MFA-fixture.md)已验证的 flow 结构，但使用独立 alias，例如 `stage-human-mfa-v1`、`stage-human-mfa-password-v1`、`stage-human-mfa-totp-v1`，避免覆盖 Keycloak 内置 flow。

1. `POST /admin/realms/stage-development/authentication/flows` 新建 `basic-flow` 顶层及两个子 flow，读取每个响应的 `Location` 获取 ID。
2. `POST .../authentication/config` 新建四个独立 alias 配置：
   - 密码 LoA：`loa-condition-level=1`、`loa-max-age=0`；
   - OTP LoA：`loa-condition-level=2`、`loa-max-age=0`；
   - 密码 AMR：`default.reference.value=pwd`、`default.reference.maxAge=600`；
   - OTP AMR：`default.reference.value=otp`、`default.reference.maxAge=600`。
3. `POST .../authentication/executions`，使用明确的 `parentFlow`、`flowId`、`authenticatorFlow`、`priority`、`requirement` 创建六个 execution：顶层分别 CONDITIONAL 引用密码/OTP flow；每个子 flow 内 REQUIRED LoA 条件，随后 REQUIRED 密码或 OTP authenticator。execution 的 `authenticatorConfig` 使用上一步 alias。
4. 读取 `GET .../authentication/flows/{alias}/executions` 和 config，确认执行数量、顺序、要求以及 alias 精确一致后，才继续人员凭据和 client 绑定。构建中途失败时，新 flow 尚未被使用，既有登录路径不变。

后台 REST 的 [`AuthenticationManagementResource`](https://github.com/keycloak/keycloak/blob/26.4.7/services/src/main/java/org/keycloak/services/resources/admin/AuthenticationManagementResource.java) 明确支持上述操作。不能只传包含 executions 的 flow JSON 给 createFlow 并假设 execution 自动建立；必须分别创建。

## 给现有测试人员追加 OTP

在私有操作文件中生成随机 OTP 种子和固定 credential UUID，并使用：

```text
PUT /admin/realms/stage-development/users/{existing-human-subject}
```

请求携带原用户表示并仅设置 `credentials` 为新 OTP 项：`id` 为本轮固定 UUID、`type=otp`、专用 `userLabel`、`secretData` 为私有种子、`credentialData` 为 TOTP/HmacSHA1/6/30。**不要携带新的 password value 或删除旧 password credential。**

26.4.7 的 [`UserResource.updateUser`](https://github.com/keycloak/keycloak/blob/26.4.7/services/src/main/java/org/keycloak/services/resources/admin/UserResource.java) 调用 [`RepresentationToModel.createCredentials`](https://github.com/keycloak/keycloak/blob/26.4.7/server-spi-private/src/main/java/org/keycloak/models/utils/RepresentationToModel.java)：已存在的 credential ID 跳过；未知 ID 通过 provider 新增；不会因为本次列表只含 OTP 就删除现有 password。固定 UUID 能防止相同操作重复追加 OTP。变更后重新读取 credentials，确认 password ID 保留且只有一个本轮 OTP ID。

这是对虚构本机账号的显式测试种子安装，不能复用为产品的人员 OTP 开通流程。正式人员应在 IdP 自助绑定并完成真实持有证明。

## 绑定 MFA client，验证后启用 Stage 策略

更新人员 client 的原表示，保留所有不相关字段，只添加/调整：

- `authenticationFlowBindingOverrides.browser` 指向新顶层 flow ID；保持其他 override。
- `attributes.acr.loa.map` 精确映射 `pwd=1,mfa=2`。
- `attributes.minimum.acr.value=mfa`、`attributes.default.acr.values=mfa`。
- `oidc-acr-mapper` 和 `oidc-amr-mapper`，读取实际 flow 结果；不添加固定 ACR/AMR/auth_time。
- 保留 `basic` scope、原 Stage audience、`stage_credential_kind`、现有 profile 所需其他 claim。

先检查已有默认 `acr` scope：若已有同类 mapper，明确只保留一个产生 ACR 的来源，避免多个 mapper 覆盖同一 claim。对完成状态重复执行时应校验配置完全匹配；部分安装只恢复本轮独立 alias，不覆盖已有未知配置。任何 client 最终绑定动作之前都应有完整旧 client 快照。

实际验收必须使用旧人员 ID、旧密码、旧 client ID、旧 issuer 和真实授权码＋PKCE：仅密码停在 OTP 页，错误/缺失 OTP 无 code，成功 OTP 产生 `acr=mfa`、`amr` 包含 `pwd/otp`、真实 `auth_time`。刷新跨秒仍保持登录时刻；旧 service 凭据和 subject 不变。随后才通过 Stage 受控策略操作启用 MFA，确认旧弱 access token/profile 被组织策略拒绝、新强登录可调用本人信息/任职查询。浏览器登录 smoke/helper 应同步支持既有 fixture 的 MFA，不能保留只会提交密码的旧脚本却声称在线回归已通过。

不增加弱 client 到这个既有 realm。弱凭据拒绝测试使用启用前获取的旧 token，或使用独立 `18082` fixture；不能为了测试而给现行身份准入建立一条弱认证入口。
