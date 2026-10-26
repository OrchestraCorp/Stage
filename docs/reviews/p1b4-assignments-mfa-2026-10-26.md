---
title: P1b.4 本人任职发现与密码加 TOTP 登录 MFA
status: server-implemented-and-verified-client-integration-pending
review_date: 2026-10-26
policy_effect: none
---

# P1b.4 本人任职发现与密码加 TOTP 登录 MFA

本轮用户授权完成受保护的本人任职查询、MFA 受控配置与强度/新鲜度校验，并明确选择首个因素组合为**密码＋TOTP**。两项服务端功能已接通，真实隔离 IdP 与 PostgreSQL 链路通过，本机常驻开发实例也已启用统一 MFA 要求。Maestro 实际桌面交互、真实人员扫码绑定/换机恢复和生产身份接入仍需各自验收。

## 1. 本人任职发现

新增 `GET /api/v1/identity/assignments`，operationId 为 `identity_list_self_assignments`。query 仅含 `profile_ref`、`profile_version`、`limit`（默认 20，上限 50）与 `after`，不接受人员/组织选择，也不要求提前提交 assignment_ref。全部成功/错误响应沿身份边界 `no-store`。

服务端先认证本人并核验当前组织 MFA 策略，再按 trust/principal→组织 guard 取得锁，读取当前本人、当前 membership 经历的有效任职，复用工作项 loader 的固定角色 revision、完整 action catalog、摘要/策略和 scope 祖先校验。返回前再次验证身份、政策与时间，撤权或到期不会被旧结果绕过。

投影只包含 `assignment_ref`、role ref/revision/code/display_name、`scope_ref`、valid_from/expires_at、行 version。它是任职选择项，没有合并动作集或长期 allow 票据；后续业务操作仍明确选择一个任职并重新授权。

分页每次最多取 `limit+1` 个候选，仅检查前 limit 个，不为了填满列表无限扫描。过滤失效角色/scope 后可能 `items=[]` 而 `next_after` 非空，客户端须继续翻页；分页不是跨请求一致性快照。没有任职正常返回空列表，不自动开权。

实现入口：[assignments loader](../../app/authz/assignments.py)、[provider](../../app/providers/assignments.py)、[API](../../app/api/assignments.py)；协议细节见[Maestro 合同](../maestro-stage-p1b-handoff.md)。新读取路径使用身份公共服务及短事务，不依赖 CommandKernel 私有授权方法。

## 2. 组织统一 MFA 策略

新增第 **26** 张应用表 `organization_human_authentication_policy`，Alembic head **`9c2e7a4b6d10`**。一组织一行，包含 version、disabled/required 模式、精确 profile/version 的规则、required_acr/required_amr 与最大认证年龄。

组织规则先约束可接受的 profile；客户端不能通过选择仍有效的旧弱 profile 绕过 MFA。ACR 按已登记语义精确匹配，不按字符串/数值大小推导等级；AMR 要包含配置的方法，新鲜度使用真实 `auth_time`，不使用 JWT 的 iat。refresh 不会重置登录时刻，也不能凭新 token 获得原先未执行的第二因素。

这项检查覆盖所有当前 human 入口：本人身份、任职发现、工作项读写、原键结果恢复。技术 service 的自身身份读取保持独立，不能拿 service token 进入人员业务。有效 token 未满足当前强度/年龄时返回 `401 MFA_REQUIRED`，challenge 为 `Bearer error="insufficient_user_authentication"`；损坏或不可读取的 policy 不会降级成 disabled。

短期内部证据固定 policy version/digest；其 valid_until 为 token、证据、成员/来源和认证新鲜度截止的最小值。命令持锁后和提交前复验政策，策略变更或认证过期阻断旧证据提交。审计仅保存必要 policy 引用、摘要、ACR/AMR、认证时间，不存 bearer token、密码或 OTP 种子。

迁移为已有组织回填 disabled/v1；没有配置行的旧式 bootstrap 保持明确 disabled/v0 兼容状态，不能将数据库升级声称为启用 MFA。实际启用必须通过[受控管理命令](../login-mfa-development.md)：环境/组织/版本校验、精确 admitted human profile、trust→组织锁序，policy 更新、epoch 与操作 journal 同事务。关闭 MFA 也需显式新版本；重放旧操作不恢复旧策略，downgrade 拒绝丢弃已配置政策。

## 3. 真实 IdP 与常驻开发实例

首先在独立 `18082` Keycloak 26.4.7 / H2 fixture 验证实际密码与 TOTP flow。ACR 来自实际 LoA，AMR 来自成功完成的 authenticator execution，auth_time 来自真实登录；没有硬编码“已经 MFA”的 claim。隔离协议测试覆盖弱认证、弱 refresh、旧弱会话/低 ACR 请求、缺失/错误 OTP、成功 PKCE/nonce/MFA、强刷新保留 auth_time，以及禁用 password grant，共 9 项通过。

随后在常驻 `18080/stage-development` 上执行有限开发升级：

- 旧 bootstrap admin 无法认证，先停止该 IdP 并备份完整 H2 data，再创建唯一临时管理用户，恢复原服务。没有重置既有管理员或覆盖 realm。
- [升级脚本](../../scripts/identity_mfa_upgrade.py) 先持久化私有 UUID 计划，创建独立 flow/config/execution，只给 `maestro-stage-dev` 配置 MFA browser override；为原虚构人员追加一次 owned OTP。
- 人员 subject、原密码 credential、issuer/client/audience、service client 与全 realm browserFlow 均保持原值；脚本完成后重复执行验证没有重复 OTP 或配置写入。
- PostgreSQL 已有私有备份，升级到新 head 后，通过 `configure.human-authentication` 启用 `stage-development` 策略 **version 2 / required**：`local-keycloak-human@1`、ACR `mfa`、AMR `pwd/otp`、最大认证年龄 **900 秒**。这是当前开发策略，不是企业生产默认政策。
- 现场保留在进程内的旧密码单因素 access token 在启用后被 `MFA_REQUIRED` 拒绝；新密码＋TOTP token 访问 `/identity/me` 和 `/identity/assignments` 为 200。常驻开发库没有业务任职，因此任职列表为空，不自动授予角色。
- 精确删除本轮临时 master 管理用户，验证其凭据不能再换取 token，再删除本地临时密码文件。备份和 owned 恢复计划保留于仓库外 0700/0600 私有目录。

私有回退材料位于 `/root/.local/state/orchestra-stage/identity/mfa-upgrade-2026-10-26/`；PG 备份为同级 `stage-before-mfa-2026-10-26.dump`。它们包含受限材料，不能提交或粘贴。升级动作与检查结果不包含用户 token。TOTP fixture 种子由开发工具预登记，不声称真实人员已经完成扫码绑定或恢复验证。

原 [identity_idp_smoke.py](../../scripts/identity_idp_smoke.py) 已支持真实 OTP 页，常驻 human/service 的 18 项协议检查在强 MFA 配置下通过。首次直接脚本运行暴露 helper 导入路径缺失，已补入口路径并重新验证成功，未把失败计入通过。

## 4. 验证结果

| 验证 | 最终结果与范围 |
|---|---|
| 本地 SQLite 快测 | **845 passed，34 skipped**，277.43 秒；不作为 PG 锁证据 |
| PostgreSQL 17＋Redis 完整回归 | **880 passed，7 skipped**，214.13 秒；跳过 5 项在线服务冒烟和 2 项需显式真实 IdP 凭据的集成，后续分别运行 |
| Core 场景映射 | **46/46**，来自上述 JUnit，见[本轮机器证据](p1b4-runtime-conformance-2026-10-26.json) |
| 真实 IdP / HTTP / PG 集成 | 最终 **2 passed**，7.32 秒：既有人员编辑/恢复/注销边界，以及新增真实 TOTP→任职发现→编辑→弱 profile 拒绝→refresh 新鲜度 |
| MFA PG 锁竞争 | 政策先、命令先与 trust→组织锁序 **3 项通过**，已包含于完整回归；早期与真实 MFA 主用例合跑为 4 passed，不重复累计 |
| 隔离真实 MFA 协议 | **9 项通过**，密码/TOTP实际 flow、ACR/AMR、refresh/auth_time、失败因素及禁用 password grant |
| 常驻开发环境 | MFA required/version 2；旧弱 token 明确拒绝、新强 token 的本人身份/任职返回 200；实际 OpenAPI 匹配；原身份协议 **18 项通过**，在线只读冒烟 **5 passed** |
| 迁移 | 独立 PostgreSQL 空库 upgrade/check/downgrade base/upgrade/check 全周期通过；常驻库 head `9c2e7a4b6d10`，Alembic check 无差异 |
| 静态与合同 | Ruff、158 个文件格式、155 个源文件 mypy、OpenAPI 合同及 diff 检查通过；任职选择合同与 MFA 错误行为有对应测试 |
| 文档与图 | 60 份 Markdown 的本地引用、锚点、围栏与当前图源同步通过；目标 SVG/PNG 已重导出并核对 |

独立真实 HTTP→PG 用例证明：弱 profile 在启用前可访问，策略开启后即使 profile/binding 仍有效也拒绝；真实 TOTP 登录可发现任职并完成编辑；弱 refresh 不能升强度；缩短新鲜度后，刷新 token 不能恢复过期认证。PG 三项竞争以阻塞 PID 证明政策先/命令先的提交顺序及 trust→组织锁序。

结果有重叠，不累计为额外功能覆盖。当前常驻开发库 role_definition/role_assignment/work_item/command_execution 均仍为 0；只有显式 MFA 配置及其操作 journal 改变，没有自动开业务权限。systemd Stage 与常驻 IdP 均 active/running、NRestarts=0；临时管理用户删除与凭据失效已验证。

验收结束后停止本轮独立 `18082` 测试进程，复跑该 opt-in 路径需按隔离 fixture 手册启动；常驻 `18080` IdP 与 Stage 不依赖它。

## 5. 身份提供方的讨论结论

本轮没有实现 OAuth/OIDC token 发行端点。[职责讨论](../identity-provider-position.md)建议：OrchestraCorp 可交付统一登录和私有化身份组件，成熟 IdP 承担 OAuth AS/OIDC OP，Stage 保持资源服务器与组织业务授权责任。该建议供继续讨论，不把用户提出探讨理解成已授权新建发行协议平台。

后续仍须完成 Maestro 真实浏览器/回调、凭据保管、MFA 错误与重新认证、任职分页选择和原键恢复。IdP 外部注销依然不是即时本地 JWT 撤销；MFA 不自动形成某次业务批准，也不替代正式人员管理或生产恢复方案。
