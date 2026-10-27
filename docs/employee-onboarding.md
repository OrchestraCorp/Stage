# 员工开户与一次性激活

状态：2026-10-27 服务端实现及隔离 PostgreSQL／Chromium 验证完成，见[审计与证据](reviews/employee-onboarding-2026-10-27.md)。延续[管理中心合同](maestro-stage-administration.md)和[首管切片](human-administration-first-slice.md)，本轮只处理新人员开户、激活重发／撤销与本人设置密码及 TOTP。离职、再入职、多管理员、邮件交付、Passkey 和 Maestro 页面另行验收。

## 权限与边界

保持管理角色 revision 1 的既有四个动作。revision 2 增加 `administration.people.create`、`administration.people.activation.reissue` 和 `administration.people.activation.revoke`；须由受控本机操作明确升级现有首管任职及 authority 版本。安装迁移或启动服务不能自动扩权。管理操作沿用当前人员令牌、明确管理任职、五分钟内密码＋TOTP、版本、原幂等键及同事务审计。

开户创建全新的 Principal、成员经历、原生身份映射和预留用户名，不按用户名／邮箱附着历史身份，不授予普通或管理任职。前三者初始为 suspended，账号 pending，不得取得 OAuth 会话或业务身份。取消为终态，保留身份及用户名预约；再入职需要另外的明确协议。

## 激活流程

一张原生激活表绑定账号、精确成员经历、外部映射及当前认证来源配置。管理员获得一次展示的随机激活码，通过已核实的安全渠道人工交付本人；Stage 不自动发送消息。明文不进入数据库、审计、幂等记录、URL 或日志。管理重放和原键查询仅返回安全元数据；响应丢失后先查原键，需要新码时显式重发。

本人在 issuer 的独立页面提交激活引用、码和新密码。校验当前来源与单次证明后进入有绝对截止时间的浏览器 ceremony，并展示待验证 TOTP。密码哈希在事务外计算，浏览器证明、CSRF 和版本在锁内重新验证。TOTP 成功的单一短事务激活所有人员事实、保存凭据与恢复码哈希、消费证明并清理临时秘密；不签发 token 或 SSO。恢复码只在成功响应显示一次。完成状态可恢复查询，秘密不可重放；随后正常 OAuth 登录。

重发只适用于既未完成、也未撤销的开户，原子废弃旧码及浏览器 ceremony，重新固定当前认证配置。撤销不得成为已激活账号的密码重置通道。失效／过期证明不可继续，配置变动后不能静默改绑。激活码默认有效一小时，管理员可选择 60 秒至一天；兑换后的浏览器步骤最多五分钟且不超过原码期限。连续五次错误 TOTP 会废弃本次证明，须管理员重发。过期或来源失效在下一次有效流程访问时清理临时秘密，重发／撤销立即清理；本轮未增加后台清理 Worker。

## Maestro API 合同

所有管理入口要求 Bearer token、`profile_ref`、`profile_version` 和明确的管理 `assignment_ref`；POST 还需 `Idempotency-Key` 与 `expected_authority_version`。调用前通过 `/api/v1/administration/me` 获取当前 authority 版本、管理任职行版本、角色 revision 和 actions。revision 1 不显示开户能力；显式升级操作见[受控启用手册](../deployment/identity/onboarding-capabilities.md)。

| 方法与路径（前缀 `/api/v1/administration`） | 输入／结果 |
|---|---|
| `POST /people` | username、expected_authority_version、expires_in_seconds、reason；返回安全 operation 与只展示一次的 activation_code、固定 issuer 页面 URL |
| `GET /onboarding/activations/{activation_ref}` | 当前激活状态、version、expires_at、principal/membership/account 引用；无秘密 |
| `POST /onboarding/activations/{activation_ref}/reissue` | expected_activation_version、期限、原因；新码只展示一次 |
| `POST /onboarding/activations/{activation_ref}/revoke` | expected_activation_version、原因；取消尚未完成的开户 |
| `GET /onboarding/operations/{idempotency_key}` | 当前获权人员查询自己原键的历史安全结果 |
| `GET /onboarding/audit` | 有界分页的开户／重发／撤销审计 |

开户操作使用 `administration.onboarding.v1` 幂等命名空间，与原任职管理的 `administration.command.v1` 分开；客户端必须保留具体入口、实例、原键与原 payload，不只保存键字符串。`503 COMMIT_OUTCOME_UNKNOWN` 的 `Location` 指向开户命名空间；重试成功只返回 operation，activation_code 为 null。恢复查询是历史提交结果，当前状态需另读激活对象；issuer 开始兑换也会增加激活 version，旧管理页面必须重新读取后明确重发／撤销意图。

管理列表的 `issued/enrolling` 是持久流程状态，`expires_at` 是邀请外层期限，不保证浏览器步骤此时仍可继续。浏览器还有五分钟期限、OTP 预算和当前来源约束；失效清理不会伪造 completed/revoked。Maestro 应显示待激活及邀请期限，遇到员工无法继续时明确重发，不将 `enrolling` 解读为认证成功或保证有效。

用户名严格使用小写 ASCII 登记形式，不做邮箱推断或静默大小写转换。账号、本体、成员和映射引用由服务端生成。页面中账号归属仍需管理员按企业既有入职流程核实；持有随机激活码就是本轮交付证明，不能把码发送给模型上下文。Maestro 仅负责授权管理和一次性交付体验，密码及 TOTP 在 issuer 页面完成。

迁移 `d8e4b6c2a901` 新增 `native_account_activation`，总计39张应用表；旧 active 账号仍强制具有 password_hash，已有密码和会话不改写。任何激活事实存在时拒绝降级删除表，避免丢失完成／撤销历史。新片段增加6个管理HTTP操作和4个issuer表单／状态操作，当前总计44个。

## 事务与验收

管理写入按命令键 → Trust S → actor Principal S／target Principal X →组织 X →账号和激活行取得保护；本人激活按 Trust S →目标 Principal X →组织 X。组织锁内不能再取得 Principal 锁，也不能复用要求 active Principal 的普通账号锁入口。最终提交前重验当前授权、认证来源、人员事实及绝对期限。

验收必须覆盖旧角色零增权、未激活登录拒绝、来源／人员变更失效、单码并发兑换、重发／撤销与 TOTP 竞争、未知提交后的安全查询、真实 PostgreSQL 迁移与浏览器页面。SQLite 仅作快测。新 API 同步 OpenAPI，不将后端通过等同于 Maestro 或生产验收。
