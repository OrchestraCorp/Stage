---
title: 企业受控 OAuth/OIDC 功能完整性矩阵
status: audited-and-verified
date: 2026-10-26
policy_effect: none
---

# 企业受控 OAuth/OIDC 功能完整性矩阵

本轮“完成”指用户已确认的企业受控多应用：Maestro 和管理员登记的其他企业应用，可以完成登录、同意、获取身份、刷新、退出及撤销。不是把所有 OAuth/OIDC 可选扩展同时实施；Passkey 与身份联邦另行建设。范围依据见[实施计划](oauth-completion-plan.md)，操作入口见[客户端管理](../deployment/identity/oauth-clients.md)。

## 功能核对

| 能力 | 实现与验证边界 |
| --- | --- |
| OIDC Discovery / OAuth Server Metadata / JWKS | 两种 well-known 元数据，固定 issuer，公布实际支持的 grant、scope、response mode、认证方式、PKCE 和退出地址；不从 Host/token 动态发现信任 |
| 授权码＋PKCE | public 和 confidential 都要求 S256；client/redirect/PKCE/期限绑定、单次消费、重用撤销；授权响应含 `iss` |
| 客户端认证 | public 使用表单 client_id；confidential 使用 `client_secret_basic`；不接受 body secret 或与 Bearer 混用 |
| 回调与错误 | 精确登记的 redirect；query 与 form_post；只有核验客户端/回调后才返回协议错误，未登记地址不能成为跳板 |
| nonce / state | Code Flow 可省略，若提供需非空有界并保留；客户端仍应生成随机 nonce/state 并验证 |
| prompt / max_age / acr_values / login_hint | 支持 none/login/consent/select_account；select_account 通过重新选择账号并认证完成，未提供多账号快捷切换器；当前强度仅密码＋TOTP |
| access / ID token | RS256、类型与 audience 分离、nonce/at_hash；Stage 业务只接受 stage-api access token；ID token 不能当 API token |
| scope | openid、profile、offline_access；profile 当前仅 preferred_username，不捏造姓名、邮箱或验证状态；offline_access 要求相应同意 |
| 刷新 | 高熵不透明凭据，只存哈希，每次轮换；按代收窄 scope，不能再扩大；保留 auth_time，重用撤销整条会话族 |
| UserInfo | GET/POST Bearer；仅按实际 token scope 返回属性，支持登录专用 stage-userinfo audience |
| 受控多客户端 | 版本化登记、更新、禁用、启用、secret 轮换、幂等 journal 与结果查询；新应用默认登录专用 audience 和用户同意 |
| Stage 业务准入 | 独立显式 admit，创建不可变 profile/binding并按预期版本追加同强度 MFA 规则；不修改既有 profile、不授予任职/资源权限 |
| 浏览器 SSO | 与短期登录事务独立的 HttpOnly cookie，凭据版本、绝对期限和当前状态复核；原认证时间不被复用动作延长 |
| 用户同意 | 绑定 account/client/version/scope，可记住已授范围；拒绝、重新要求同意、客户端更新后再同意均有明确行为 |
| RP 发起退出 | GET/POST 发起、确认 POST；过期 ID token hint 可校验身份提示但不作为撤销授权；精确登出回跳、Cookie/CSRF、同浏览器各 RP 会话撤销 |
| 跨站 POST 退出 | 同源自动转交后再准备确认，解决 SameSite=Lax 首次不带 SSO Cookie；确认仍严格同源，无 GET 自动撤销 |
| 本人会话/授权管理 | 分页查询、撤销自己单个 RP 会话、全部退出、撤销单个应用授权；禁止指定他人身份，敏感操作受当前组织新鲜度约束 |
| 撤销即时性 | client version、账号凭据版本、RP/browser session 当前状态在持锁后复核；重新启用客户端不会复活旧 token |
| 异常与日志 | 单次消费提交未知不盲重试；access 日志去掉查询串，避免 logout hint/state 进入本机日志；应用错误不回显秘密 |
| 限流 | 登录因素、授权请求、退出发起分开持久计数；来源预算和窗口可配置，账号失败锁定及 Argon2 并发预算继续独立生效 |

## 访问权的三个边界

客户端登记只决定哪些应用可使用发行器。默认 `stage-userinfo` token 用于登录身份，不能进入 Stage 业务 API，也不能进入账户安全/会话管理入口。

管理员显式把客户端设为 `stage-api` audience 后，该应用可代表已认证人员调用本人的账户安全入口；这些入口要求当前原生账号/会话及敏感操作的新鲜度，不要求业务岗位。进入 `/identity/*` 或工作项等业务链，还必须完成独立 profile/binding/MFA 来源准入，再按当前成员、任职、关系和对象状态判断。不能把 OAuth scope、client secret 或同意页面当成业务权限。

浏览器退出只撤销当前浏览器关联的会话；单 token revoke 撤销单个会话族；本人“全部退出”增加凭据版本并撤销全部 RP/browser 会话和待完成登录。三者不混为一个动作。无 SSO Cookie 时，合法 logout hint 可用于确认后的登记回跳，但不能据此撤销另一浏览器的会话；RP 应同时撤销自己持有的 refresh。

## 明确未包含的能力

| 项目 | 当前处理 |
| --- | --- |
| 公开自动客户端注册、管理 UI/公开管理 API | 不开放；由受控 dev/test OS 工具登记，生产管理职责另验 |
| Passkey / WebAuthn、LDAP/外部 IdP 联邦 | 独立后续能力 |
| email/name 等个人资料 scope | 尚无对应权威资料与验证流程，不返回虚构 claims |
| client_credentials、Device Flow、JWT Bearer grant、Token Exchange | 未支持；不能因此创建独立 Agent 业务主体 |
| implicit / password grant | 明确禁用 |
| 跨域 SPA、iframe Session Management、front/back-channel logout通知 | 未支持；SSO 验证范围为顶层浏览器导航，Web 应用采用服务端/BFF |
| JAR/PAR/FAPI、官方 OIDC 一致性认证 | 未宣称；本轮用标准 Authlib 客户端、真实浏览器及安全边界回归验证选定合同 |
| 在线 key 热撤销、多节点滚动、自动历史清理 | 未交付；密钥整体部署轮换与持久事实的受控恢复仍按已有手册 |
| 生产容量/恢复与实际 Maestro 产品交互 | 单独验收；正确性并发测试不是吞吐承诺 |

## 客户端接入注意

业务 API 的 Bearer header 不能自动附加到 `/oauth/token` 或 `/oauth/revoke`；应按该客户端的认证方式单独发送。刷新必须单飞并原子替换本地安全存储，网络未知结果不循环重放旧 code/refresh。

query 是默认回调；form_post 要求 RP 接收跨来源 POST，并正确处理自身 state 与 Cookie 的 SameSite 策略。跨站 POST 退出的转交页只回本站且没有撤销副作用，真正退出仍需确认。入口代理和 RP 回调访问日志也必须去掉查询串、令牌和请求正文；本机 Uvicorn 日志过滤不能代替外部代理配置。

限流来源由 HTTP server 的可信代理配置决定，应用不自行接受任意 X-Forwarded-For。默认每个来源 300 次/600 秒的独立阶段预算可以显式调整；账号失败锁定仍是独立边界。部署规模增加前，应以企业共享出口和实际代理拓扑做容量标定。


## 实测结果

本轮最终证据如下：

| 验证 | 结果 |
| --- | --- |
| PostgreSQL 全量回归（迁移、既有身份、授权、OAuth） | **1383 passed / 9 skipped**；Authlib warning 仅来自测试客户端的已弃用导入 |
| SQLite 全量快测 | **1332 passed / 52 skipped** |
| 最新 PostgreSQL 原生/OAuth/浏览器定向组 | **551 passed** |
| 客户端、退出、Cookie 主机隔离、provider 部署校验定向组 | **166 + 11 + 46 passed**；PG 专项锁测试按环境标记 |
| Authlib public/confidential 互操作 | 两条真实 ASGI HTTP 全链路通过 |
| Chromium 浏览器 | 原生重绑 14 项；受控 OAuth/SSO/同意/form_post/RP logout **11 项** |
| Runtime Core | **46/46** |
| 常驻 Stage | 9 项 native smoke、5 项探针/并发/合同/PG/Redis；重启计数 0，ready |
| 静态与合同 | Ruff、Mypy **204 文件**、OpenAPI、文档链接与 Mermaid 通过 |

本机当前 head 为 `c6f3a19d820b`，37 张应用表。迁移前测试账号的访问 token/refresh 仍兼容，账号和 TOTP 事实摘要保持一致；新 OAuth 客户端默认不取得 Stage 业务准入。认证 issuer、provider、trust、组织和 bootstrap 环境不一致时，native provider 在启动阶段拒绝发布服务。生产 HTTPS 还要求认证 issuer 使用独立主机名，避免 Cookie 按主机匹配而跨端口泄露给 RP。
