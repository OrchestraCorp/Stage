# 人员登录 MFA：组织策略与开发管理

状态：2026-10-26 用户已选密码＋TOTP；组织策略与服务端路径已实现，真实隔离 IdP/Stage 验证见[P1b.4 记录](reviews/p1b4-assignments-mfa-2026-10-26.md)。随后[原生认证模块](native-authentication-design.md)已实现服务端因素绑定与恢复；Maestro 桌面与生产恢复流程另验。本页的命令只适用于 dev/test，不是生产身份管理 API。

## 职责与信任边界

当前原生认证模块负责密码、TOTP、认证器绑定、恢复和会话，种子加密存储、主密钥在数据库外，按最小 OAuth/OIDC 签发 token；Keycloak adapter 保留历史兼容。IdentityService 验证 access token 并对原生来源重验当前本地会话，在所有人员入口执行组织统一的认证强度和新鲜度要求。Passkey 未列为本轮登录实现；登录 MFA 不等于某个业务意图的批准或签名。下文组织策略与受控配置对两类来源均适用，历史 Keycloak 示例不代表当前默认来源。

`acr` 的含义必须由具体 IdP 和认证 profile 确认。不同 IdP 的同名 `acr` 不自动等价；`amr` 是认证方法集合，不按元素个数计算因素数量。不能添加一个始终返回 `acr=2` 的固定 claim mapper 来伪装实际 OTP 验证。

组织策略的规则精确绑定 `profile_ref + profile_version`。启用 `required` 后，未列出的旧 profile 也被拒绝；列出的 profile 仍须满足该规则的 ACR、AMR 和认证年龄。因此客户端换一个 profile 参数不能降低要求。技术服务认证不获得人员权限，单独遵循原有服务准入。

## 保存和执行的策略

新增 `organization_human_authentication_policy` 表，以本实例组织根为主键，保存：

| 字段 | 含义 |
| --- | --- |
| `organization_ref` | 固定组织授权根，外键指向 `organization_root` |
| `version` | 每次显式配置递增；参与短期人员认证证据重验；证据同时固定策略内容摘要 |
| `mode` | `disabled` 或 `required` |
| `rules_json` | 精确 profile/version 对应的 required ACR 与可选 required AMR |
| `max_auth_age_seconds` | 从已验证 `auth_time` 起算的最大认证年龄；required 时为 1–86400 秒 |
| `created_at` / `updated_at` | 配置行时间 |

`required` 必须配置 1–32 条互不重复的 profile 规则和认证年龄。每条规则必须有 `required_acr`，`required_amr` 可为空，是否要求具体方法由该 IdP 的已核验协议决定。`disabled` 必须没有规则和认证年龄。

现有 profile/来源准入、成员状态、任职和对象授权继续独立检查。加强登录认证不会授予新的角色或工作项权限。刷新 access token 的签发时间不能替代原登录的 `auth_time`。

强度或新鲜度不足时，身份、任职与工作项入口统一返回 401 / `MFA_REQUIRED` 和 `WWW-Authenticate: Bearer error="insufficient_user_authentication"`。

策略变更使用组织 guard，与人员操作的授权及提交排序。提交前还检查策略版本和认证年龄；持有旧证据的操作必须重新认证，不能把读取过的策略当成永久通行证。

迁移 `9c2e7a4b6d10` 将已有组织根回填为 `disabled/version=1`，不自动宣布其完成 MFA。迁移后首次 bootstrap 的新组织暂允许策略缺行，明确等价于 `disabled/version=0`；首次显式配置时创建行。数据库查询错误或已存在但无法解释的策略不能按缺行降级。

## 显式配置

先在 IdP 确认实际强认证流程，并验证 Stage profile/来源准入已经安装。每个规则对应的 profile 必须是当前有效的 `human_oidc` profile，内容摘要一致，provider/trust 和 dev/test 来源绑定有效。配置操作不会自动创建 profile、绑定、成员或业务授权。

以下是示意计划，组织引用、profile 和强度语义都应替换成当前部署的已核验值。迁移后已有组织的首次配置通常使用 `expected_version=1`；新 bootstrap 且无策略行时为 `0`。应先由受控运维检查当前版本，不能据此猜测生产状态。

```json
{
  "environment": "dev",
  "organization_ref": "organization:example",
  "expected_version": 1,
  "policy": {
    "mode": "required",
    "rules": [
      {
        "profile_ref": "profile:example-human",
        "profile_version": 1,
        "required_acr": "2",
        "required_amr": ["pwd", "otp"]
      }
    ],
    "max_auth_age_seconds": 3600
  }
}
```

在显式设置 `STAGE_DATABASE_URL`、`STAGE_ENV=dev` 的受控本机环境中执行：

```bash
uv run python scripts/identity_admin.py --environment dev configure-human-authentication \
  --plan /private/path/human-assurance.json \
  --idempotency-key enable-login-mfa-001
```

CLI 同时核对运行环境、计划环境与数据库 bootstrap 环境。操作者身份来自当前主机和 OS UID，不能在命令参数里伪装成某个业务人员。

配置行、组织授权 epoch 和 `identity_control_operation` 中的前后状态、结果在同一事务内提交。错误版本不会覆盖新配置；相同操作者、相同键、相同输入返回原结果，不重复递增版本；相同键不同输入返回冲突。

若提交结果未知，使用原 OS 操作者和原幂等键恢复：

```bash
uv run python scripts/identity_admin.py --environment dev result \
  --operation-type configure.human-authentication \
  --idempotency-key enable-login-mfa-001
```

原操作结果只是历史提交证明，不是当前配置视图。后续关闭或变更策略后，重放旧启用操作不会重新启用旧配置。查不到原结果也不能证明从未提交；需要重试时仍使用原键和原输入。

关闭要求必须显式提交新计划，例如 `policy={"mode":"disabled"}`，并提供当前 `expected_version` 和新幂等键。它同样递增版本、epoch 并留下审计。不能通过删除规则、选择旧 profile、重放旧命令或回退数据库迁移隐式关闭 MFA。迁移 downgrade 会拒绝丢弃已配置的策略及其配置历史。

## 客户端接缝

Maestro 在首次认证前从可信部署配置获得 IdP、client、profile 以及要求的认证等级。需要加强登录时，通过 OIDC 的 `acr_values`、`max_age` 或实际 IdP 支持的等价参数发起浏览器授权码 + PKCE 流程，并处理用户取消、OTP 错误和重新登录。

这些参数只是请求要求；Stage 最终以签名 token、固定 profile 和当前组织策略为准。本人任职发现是登录后的受保护查询，不能替代首次登录前的可信配置。Stage 不接受客户端自行声明“已完成 MFA”的请求字段。

本轮策略不实现账户恢复、因子自助管理、生产 IdP 管理、远端注销即时通知或业务意图批准。TOTP 的真实流程验证和 Maestro 桌面浏览器验收应分别记录，不以读取到 `acr/amr` 宣称全部完成。
