---
title: 原生认证详细复测与深度架构审计
status: audited-and-verified
date: 2026-10-26
policy_effect: none
---

# 原生认证详细复测与深度架构审计

本轮在初次交付后进行独立审计，范围是原生凭据、OAuth/OIDC/HTTP、现有人员身份和业务授权、可信 Runtime provider、表结构迁移及操作工具。先构造反例，再实施局部修复；并未把原有大量测试通过当成不存在缺陷的证明。原交付记录保留为历史证据，[现行设计](../native-authentication-design.md)采用本轮收紧后的合同。

## 1. 结论

“本地账号与会话 → 当前人员/成员准入 → 当前单任职与资源授权 → 受控业务事务”这一分工成立，适合作为当前企业私有化一期的服务端底座。原生账号没有成为独立业务授权主体，签名 token 的角色或权限声明不能直接授予业务能力；同一实例仍只有一个组织授权根。

审计发现并修复一项 P1 安全问题及四项 P2 边界/配置问题。修复后可继续推进 Maestro 联调，尚不能据此宣布通用 IdP、生产恢复、企业登录峰值或多节点部署已经验收。组织 guard 与现有限流的容量约束是真实限制，需要安排在扩大使用范围前处理。

## 2. 可复现问题与修复

| 优先级 | 触发与影响 | 修复和证据 |
| --- | --- | --- |
| P1 | 恢复/重置清空 TOTP 后，另一浏览器仅凭密码即可领取新种子，抢先绑定并使合法恢复请求失效 | 新增明确的 `totp_bootstrap_allowed`；既有因素替换必须持恢复码或单次重绑码。独立双浏览器反例、HTTP 完整替换流程及 PG 并发竞争均纳入回归 |
| P2 | OTP/恢复在检查请求期限后继续等待数据库，过期请求仍可提交因素、版本和会话撤销 | flush 后、commit 前重验浏览器请求期限及密码证明年龄；回归证明超期完整回滚，测试等待在 HTTP 总超时以内 |
| P2 | 工作项读取授权后又等待数据，认证/MFA/任职/关系在最后一次等待中到期仍可能返回内容 | 最后数据读取后重验完整授权，保持原锁序；四类到期回归覆盖。结果恢复原有“先读行、后授权”顺序正确，另外三项回归确认无需修改 Kernel |
| P2 | issuer 接受空 `?/#` 或与浏览器 Origin 规范化不同的拼写，协议路径失效或正常登录被 403 | 启动时要求规范 origin，保持严格 Origin 校验；覆盖大写主机、显式默认端口、非规范 IPv4/IPv6、zone ID 等 |
| P2 | 安装接受字符数合规但 UTF-8 超过 128 字节的 account/client 引用，签发后会被身份 adapter 拒绝 | 安装和账号创建对齐 token adapter 的字节预算，保留预算内 Unicode；新增跨边界注册反例 |

另修复一个操作工具问题：私有计划读取现在以 non-blocking 方式打开，再校验 regular file；误把命名管道作为计划时立即拒绝，不无限等待写端。这不属于网络认证绕过。

相关实现：

- [凭据状态机](../../app/native_auth/credentials.py)、[增量迁移](../../migrations/versions/b7f0d3a9216c_native_factor_enrollment_authority.py)。
- [HTTP 合同](../../app/api/native_auth.py)、[发行配置](../../app/native_auth/config.py)、[安装准入](../../app/native_auth/administration.py)。
- [业务读取边界](../../app/providers/work.py)、[操作者工具](../../scripts/native_auth_admin.py)。
- 独立反例：[凭据](../../tests/native_auth/test_audit_credentials.py)、[协议配置](../../tests/native_auth/test_audit_protocol.py)、[身份与授权](../../tests/native_auth/test_audit_identity.py)、[操作工具](../../tests/native_auth/test_audit_administration.py)。

## 3. 重绑状态和 API 合同

首次安装的账号可以在证明密码后绑定首个 TOTP；首次绑定成功后永久退出该引导状态。恢复码进入重绑时，消费当前码并撤销旧会话，剩余未消费码可供中断后重新发起；新的 TOTP 完成后替换整批恢复码。不同浏览器的过时请求由凭据版本拒绝，密码本身不再等价于重绑权限。

`POST /api/v1/account/totp/reset` 仍要求近期原生强认证、密码和未使用 OTP，成功响应从 204 收紧为 **200 `{credential_version, enrollment_code}`**。旧会话立即撤销；客户端须保存这次仅返回一次的码，重新密码登录后在恢复表单提交，再实际验证新 TOTP。丢失响应不能重复旧请求取码，更不能允许密码直接重绑。

操作员 `reset_totp=true` 同样产生重绑证明，必须通过 `--enrollment-output` 写入新的 0600 文件；控制台不打印秘密。输出目标不可创建时不改变凭据；提交后文件持久化失败返回未知结果，必须先 inspect 当前版本，再明确处理。纯密码重置不返回重绑码。

初次因素审计 head 为 `b7f0d3a9216c`、34 张表；本轮企业 OAuth 扩展迁移 `c6f3a19d820b` 增加 browser session、consent、logout request，当前 head 为 `c6f3a19d820b`、37 张表。已有种子或凭据版本大于 1 的账号被保守标为不允许密码引导；含账号事实时禁止降级丢弃该安全状态。迁移不修改现有密码摘要、TOTP 种子、人员/成员引用或任职。

## 4. 架构判断与演进边界

| 层次 | 当前判断 | 后续要求 |
| --- | --- | --- |
| 凭据与业务身份 | 分表、精确账号到 Principal 映射合理；不是邮箱自动关联，登录不自动入职或授予岗位 | 人员/权限管理产品按独立最小切片补齐，不能让 IdP 角色替代当前业务授权 |
| 认证与授权 | 原生 token 验签后仍验证 session/account/client/version，业务提交另查任职、关系和组织 MFA | 保留统一强制入口；原生 profile 与 Keycloak profile 不互换；来源撤销与账号停用分别管理 |
| 持久与事务 | PostgreSQL 权威保存一次性消费和撤销，Redis 不承担正确性；重放与提交未知没有返回旧明文秘密 | 所有新增凭据状态都要定义期限、撤销、未知结果和迁移，不靠缓存或文档约定弥补 |
| Runtime/Cordis 参考 | provider 使用明确依赖、lease 和生命周期，基础身份/事务边界稳定，业务代码可以组合 | 它是可信模块生命周期机制，不是 Python 插件安全沙箱；不需要为了“万物皆插件”扩大运行时 |
| 锁与容量 | 写入/撤销锁顺序一致；业务读末尾重验补齐时间边界 | 原生 UserInfo/refresh 等也占 principal 排他锁和组织 guard，会与业务竞争；先测试真实负载，再细化协调协议，不能直接删除锁 |
| 发行器规模 | 预登记第一方 public client、固定 Stage audience 与有限 scope 符合当前范围 | 增加外部系统统一登录前，另验客户端生命周期、授权/同意、协议一致性和故障恢复；不能称完整 OAuth/OIDC 平台 |

Authlib 当前复用的是 PKCE S256 等基础辅助函数，未使用其完整 AuthorizationServer。凭据恢复、协议消费与会话事务状态机仍由 Stage 自身实现和维护；引入成熟算法库不等于自动获得发行服务器的一致性认证，本轮因此对恢复、期限和并发单独构造了反例。

有两处可在后续小步整理的依赖债：原生 adapter 复用了名字带 Keycloak 的 profile DTO；identity 与 credentials 通过 OAuth 模块调用会话验证。可以提取中性的验证配置和会话验证模块，并让验证端仅持公钥视图，但当前不需要增加通用认证框架或插件系统。身份 provider 与发行 provider 都加载签名 ring；本版只能整体停止/排空、切换、启动，不能只热重载其中一个。

现有来源地址限流是每 10 分钟 30 次，密码、OTP、恢复共享预算，成功尝试也计数；一个共享出口约 15 次普通双因素登录就可能耗尽预算。这是扩大使用前的明确容量项：应一起标定成功/失败预算、可信代理与真实来源地址、并发上界和全局过载策略。Argon2 两槽预算只证明哈希计算内存有界，不能代表整套登录容量已通过。

生产前还需完成：认证事实保留/受控清理、数据库外密钥配套恢复及防止恢复旧撤销状态、明确的操作员责任、签名密钥退休、部署监控、真实 Maestro OS 凭据存储和客户端恢复交互。上述限制已存在或属于明确未交付范围，不冒充本轮已发现的越权漏洞。

## 5. 实测证据

| 检查 | 本轮结果 |
| --- | --- |
| PostgreSQL＋Redis 全量 | **1127 passed / 7 skipped** |
| SQLite 便携快测全量 | **1080 passed / 46 skipped**，不作为运行数据库 |
| 后补的注册字节预算/操作员交付回归 | PG **10 passed**；SQLite **10 passed** |
| 隔离 Chromium 完整重绑 | **1 个端到端测试通过、14 个检查通过**，真实 loopback 回调 |
| Runtime 场景映射 | **46/46** |
| 修复后常驻原生认证冒烟 | **9 项通过** |
| 常驻探针、并发探针、合同、PG/Redis | **5 passed** |
| 静态验证 | Ruff 通过，Mypy **184 文件**通过，OpenAPI 一致 |

全量中的跳过项按显式环境控制，包含旧外部 IdP/常驻服务检查及 SQLite 不支持的真库并发路径；常驻服务另行执行，真实浏览器测试也单独明确启用。全量之后增加的字节预算检查和浏览器端到端分别复测，不把重复项相加冒充独立测试总数。[机器记录](native-authentication-audit-2026-10-26.json)保留实际代码摘要与 JUnit 摘要，[Runtime 证据](native-authentication-audit-runtime-2026-10-26.json)对应具体场景。

本机部署库先做私有备份，再停止/排空 Stage、增量迁移、执行 Alembic check、重新启动。实际 head 为 `b7f0d3a9216c`；通过迁移前后摘要比对，确认原密码摘要、TOTP 种子、账号版本和人员关联完全保留，已有账号的密码引导标志为 false。Stage running，PG/Redis ready，进程自动重启计数为 0。

敏感的重置/恢复、密码抢绑与重绑码重用试验使用一次性 PostgreSQL 测试库和虚构账号；真实部署账号只做正常登录并撤销测试会话，没有重置其原有因素。Chromium 检查覆盖旧 access/refresh 失效、全新浏览器仅密码无 seed、受保护重绑码消费、新 OTP、恢复码展示、真实回调、PKCE、同一人员业务身份、已用重绑码拒绝和最终退出。



## 6. OAuth 扩展复测补充

随后按用户确认的企业受控多应用范围完成客户端登记/版本/密钥轮换、独立业务准入、public/confidential Code Flow、SSO、同意、prompt/max_age、form_post、UserInfo POST、RP logout、本人会话和授权撤销。独立审计又修复了三处边界：SSO/同意/授权码写入跨数据库等待后的新鲜度检查、code/refresh 写入等待跨过绝对期限、以及同主机不同端口的生产 SSO Cookie 泄露路径。客户端注册与运行时回跳现在共同拒绝生产同 hostname 回调；dev/test loopback 明确保留为本地例外。

最终功能矩阵见[OAuth 完整性矩阵](../oauth-functional-coverage.md)。本轮没有把公开动态注册、Passkey、外部身份联邦、机器授权、跨域 SPA 或官方一致性认证写成已完成；Stage 业务授权仍由现有身份/成员/任职/关系链决定。新客户端可默认仅获得 `stage-userinfo`，显式 `stage-api` 同时仅代表本人账户安全入口；业务 profile/binding/MFA admission 仍是另一道准入。
