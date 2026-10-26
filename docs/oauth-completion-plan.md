---
title: 企业受控 OAuth/OIDC 完成范围
status: implementing
date: 2026-10-26
policy_effect: none
---

# 企业受控 OAuth/OIDC 完成范围

本轮按用户“继续做完 OAuth，再回顾完整性”的要求，完成企业内部多应用可实际接入的发行器。首期限定的单一 public client 是上一轮验收边界；本轮扩为受控多应用，并以功能矩阵、标准客户端、真实浏览器、PostgreSQL 及撤销竞争逐项验收。Passkey 仍是独立后续能力。

## 交付合同

1. 管理员登记、更新、禁用 public/native 和 confidential/web 客户端；精确授权/登出回调、允许 scope、客户端版本和密钥轮换。管理变更有预期版本、幂等 journal 和未知结果查询，密钥只进入私有交付文件。
2. 默认接入应用只获得 `stage-userinfo` audience；访问 Stage 业务 API 必须另行显式准入，创建独立不可变 profile/binding、按预期版本更新组织 MFA allowlist，不改变人员任职或资源权限。
3. OIDC Code Flow：S256 PKCE、public/Basic 客户端认证、`openid/profile/offline_access`、可选 state/nonce、`query/form_post`、`prompt`/`max_age`/`acr_values`/`login_hint` 的明确行为、可信回调错误和 issuer 响应参数。profile 当前只披露有事实依据的 preferred_username。
4. 独立浏览器 SSO 会话，绑定账号凭据版本与绝对期限；同意记录绑定客户端版本和 scope。重认证不通过 refresh 伪造 auth_time。`prompt=none` 在缺少登录或同意时返回对应协议错误；不宣称跨站 iframe 静默登录。
5. RP 发起登出使用已签 ID token hint 校验客户端与登记回跳；GET 只展示确认，POST 使用浏览器秘密和 CSRF。浏览器登出撤销其关联的 RP 会话；token revoke 保持单个会话族语义。本人可查询/撤销自己会话，并显式退出全部会话。
6. Discovery 与实际实现一致；UserInfo 支持 GET/POST Bearer；客户端变更、会话退出和凭据变更在数据库锁协议下对旧 token 生效。

## 安全与兼容

原生账号仍映射到现有 HUMAN Principal；组织/任职/关系/属性授权继续由 Stage 当前事实判定。所有扩展保持 trust→principal→组织 guard 的顺序；客户端配置以 trust 排他锁串行化并撤销该客户端会话，不能就地修改已发布认证 profile 的客户端范围。

旧客户端迁移保留 `stage-api` audience、openid、public 认证与原准入，新注册客户端默认仅用于身份登录并要求用户同意。浏览器 cookie 与短期登录事务 cookie 分离；凭据重置/停用和全局退出使浏览器会话失效。恢复/重绑仍遵守上一轮安全审计的单次证明合同。

公开无需管理员的 RFC 7591 注册、LDAP/外部 IdP 联邦、机器 client_credentials、设备授权、跨域 SPA、JAR/PAR/FAPI、前后通道通知以及官方认证不是本轮选定的协议能力。它们不会因为“不支持所有可选扩展”阻碍受控企业 OIDC Code Flow 的完整闭环；最终矩阵必须明确区分已实现、受限制、未支持和独立部署验收，不能把本轮称为所有 OAuth 标准均已实现。
