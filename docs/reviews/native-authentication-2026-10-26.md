---
title: Stage 原生认证实现与验收
status: service-side-verified
date: 2026-10-26
policy_effect: none
---

# Stage 原生认证实现与验收

> 本页是初次交付的历史验收记录。后续[独立深度审计](native-authentication-audit-2026-10-26.md)发现并修复了因素重绑授权与到期边界，重置 API 和当前 schema 以审计后的合同为准。

本轮按照用户明确决定，先落档[最小认证设计](../native-authentication-design.md)，随后实现服务端密码＋TOTP、因素恢复与第一方最小 OAuth/OIDC。此前默认依赖独立 Keycloak 的建议保留为历史记录，不再作为现行实施约束。没有扩建通用身份联邦或独立 Agent 身份系统。

## 实现与架构

- `app/native_auth` 维护密码、因素、会话与协议状态，Argon2id、PyOTP、Authlib、PyJWT/cryptography 负责成熟算法及协议基础组件。
- `native-auth` provider 使用现有 RuntimeHost/STORAGE 生命周期，显式配置启用后进入必需依赖；HTTP 路由仍为静态注册。
- 原生 JWT 通过 `stage.native-access.v1` profile 映射到既有人员；本地会话当前状态在 IdentityService 和业务提交事务内重验，不能只凭 JWT 尚未过期执行。
- 原有单任职、角色版本、资源关系、组织 guard、幂等与审计继续约束业务操作。新增账号不授予成员资格、任职或工作项权限。
- 增量迁移 `e8a1c6d9f240` 增加八张认证表，总计 34 张；回滚拒绝删除已经产生的认证事实。

账号安全变更复用 trust→principal→组织 guard 的锁序；密码、OTP 和恢复码不进入日志或审计。公开账户变更锁后重验完整访问凭据，flush 后再校验 token/认证年龄期限；数据库提交未知不能当成安全重试的证明。授权码和 refresh 的一次性语义不复用业务命令明文结果回放。

## 验证范围

定向验证包括密码哈希与取消预算、TOTP 时间步重放、恢复码消费与重新绑定、CSRF/browser/Origin、精确客户端与回调、PKCE、ID/access token 区分、refresh 轮换与重用撤销、当前会话与凭据版本、错误体脱敏、签名密钥轮换和文件权限，以及安装幂等和数据库环境护栏。

PostgreSQL 验证一次性授权码/refresh、OTP/恢复码并发消费，以及退出、因素恢复、密码恢复分别先于或后于业务提交的六组真实锁竞争；用 `pg_blocking_pids` 观察等待关系，不用计时猜测来证明顺序。

真实 TCP 集成启动完整 Stage RuntimeHost，通过实际浏览器表单协议完成首次因素绑定、授权码＋PKCE、本人任职发现、工作项编辑、刷新后同 key 重试和退出后拒绝身份/结果读取；数据库从 Alembic head 迁移建立，确认只写一份命令与审计、无额外 outbox。

实际结果：

| 验证 | 结果 |
| --- | --- |
| PostgreSQL 17＋Redis 全量回归 | **1086 passed / 7 skipped** |
| SQLite 便携快测全量回归 | **1040 passed / 45 skipped**，SQLite 仅用于测试 |
| 浏览器修复后的 PostgreSQL 原生认证＋完整 TCP 链路 | **208 passed** |
| 浏览器修复后的 SQLite HTTP/凭据回归 | **61 passed / 2 PG 专项 skipped** |
| Chromium 实际表单与真实 loopback 回调 | **6 项通过**，回调不发送 Referer |
| 常驻服务原生协议/身份冒烟 | **9 项通过** |
| Keycloak 停止期间重复原生冒烟 | **9 项通过**，随后恢复旧参考服务 |
| 常驻服务探针/并发探针/合同/PG/Redis | **5 passed**，并发探针 16 请求 × 5 轮 |
| 静态检查 | Ruff 通过；Mypy **179 个文件**通过；OpenAPI 合同一致 |
| 文档与图 | 链接、锚点、围栏及内联图与源文件一致；Mermaid 实际渲染通过 |

全量中的跳过项是显式环境条件控制：PG 运行未启用两个旧外部 IdP 集成和五个常驻服务冒烟（后者已另行实跑）；SQLite 额外跳过真库锁/持久并发等检查。本轮修复一处仍把旧 revision 当 head 的迁移断言，迁移的保留事实/拒绝危险 downgrade 行为不变。

真实浏览器发现并修复了 HTTP 客户端模拟没有暴露的响应头问题：登录 HTML 的 `no-referrer` 会产生 `Origin: null`，改为 `same-origin` 后保留同源表单验证；`form-action 'self'` 会拦截跨来源客户端回调，改为本站加服务器已验证、持久绑定的回调 origin。路径/query 不插入 CSP，POST 不能自报回调，协议 JSON 和 303 仍使用严格 CSP 与 `no-referrer`。Chromium 实际回调监听再次验证成功；相应定向 HTTP/PG 回归已补齐。

## 本机实际状态

`orchestra-stage-dev` 仍监听 `127.0.0.1:8080`，PG/Redis readiness 均 ready。人员登录使用 `stage-native-human@1`、client `maestro-native-dev`、精确回调 `http://127.0.0.1:18765/callback`，ACR 为 `urn:orchestra:acr:pwd-totp`。组织策略经短期双来源验证后收紧为 native-only，当前 **required/v4、认证年龄 900 秒**。实际旧 Keycloak human token 被拒绝为 `MFA_REQUIRED`；旧技术服务 profile 仍仅提供原有技术身份自读，不获得人员业务权限。

原生开发账号显式关联既有人员和当前成员经历，安装结果没有业务 grants；工作项写入只在隔离测试库执行。本机迁移前的数据库和配置已备份到仓库外私有目录；密码、加密主密钥、签名材料和恢复码均未写入仓库。

已实际执行一次候选签名密钥整体部署轮换：停止并排空 Stage、替换私有 ring、重启；旧签名的未过期 token 保持可验证，刷新后的 token 使用新 active kid，JWKS 仅有新旧公钥，测试会话已撤销。没有将此结果外推为在线热撤销或多节点滚动能力。


## 仍需独立验收的边界

实际 Maestro 系统浏览器、回调监听和 OS 安全存储尚未联调。首版只支持预登记第一方 public client，默认关闭；没有动态注册、第三方应用授权平台、跨域 SPA、免交互 SSO cookie、LDAP/外部联邦或官方 OIDC 一致性认证。

签名密钥在进程启动时加载，候选密钥通过停止/排空整个 Stage、切换私有文件再启动激活；不是在线热撤销或多节点传播。数据库和数据库外加密主密钥须配套备份，数据库回滚可能使已撤销凭据重新出现，恢复流程必须重新收紧会话与凭据版本。

组织 guard 保持原有串行边界；本轮并发测试证明一致性，不是生产吞吐容量验收。没有认证历史自动清理任务、生产管理 API 或邮件找回产品。开发 CLI 不替代正式人员管理，登录 MFA 也不等于具体业务意图批准。
