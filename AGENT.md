# Stage Runtime Repository Instructions

本仓库是 OrchestraCorp Stage 的运行服务底板，不是 ScoreBook 的制度仓，也不保存真实企业治理正文。

## 权威边界

- ScoreBook 保存批准的规则、岗位契约、正式知识和发布版本。
- Stage 保存当前组织关系、任务、承诺、授权、运行、审计和实际加载记录。
- GitLab 保存实际仓库成员、可见性和分支权限；Stage 只保存期望、请求、核验和漂移状态。
- 专业业务系统保存自己的领域事实；Stage 保存必要引用与协作状态。
- Virtuoso 的私人会话和个人缓存不自动进入 Stage。

## 开发规则

1. 业务代码只调用 `app.authz` 的能力判定，不直接判断角色名称。
2. 权限、关系、属性、对象版本和业务事务分别测试；不能用一条端到端测试代替边界测试。
3. 业务写命令必须定义幂等键、对象版本、审计和失败/未知结果。认证协议的 code/refresh/OTP 消费按一次性语义处理，不通过业务幂等返回旧明文秘密；账户凭据变更采用当前会话与凭据版本重验、安全事件和明确未知结果，具体见原生认证设计。
4. 不让 Agent、连接器或配置蓝图直接写数据库；通过领域命令、outbox 和受控接口执行。
5. 不把本地测试配置、凭据或真实企业数据提交到仓库。
6. 修改 API 时同步更新 `contracts/openapi.yaml` 和契约测试。
7. PostgreSQL 17 是唯一长期主库，Redis 8 仅作可重建缓存、非权威短期协调和可丢失唤醒；具体使用须定义故障策略。授权、幂等、outbox 与业务租约权威留在 PostgreSQL，不承诺 PostgreSQL/Redis 双写原子性。
8. Redis 清空不能丢业务事实；缓存须隔离部署环境、组织与主体/作用域并核验当前授权版本，不能绕过撤权。SQLite 仅快测，MySQL 已退出受支持配置；历史迁移与验证记录保留。存储细则见 [存储策略](docs/storage-strategy.md)。

9. 产品采用企业私有化部署：每套 Stage 一个组织授权根、一套专属数据库、一个部署环境。组织授权根是信任、治理、授权 epoch 和审计边界，不等同于一个法人公司。集团、子公司、部门等业务组织单元以后按需建模；scope 是授权范围，不自动等同组织树。现有 `tenant_*` 表/字段保留兼容，表示服务端固定的组织根；禁止客户端选择组织或新增 SaaS 租户管理入口。详见 [组织授权根语义](docs/organization-root-semantics.md)。
10. 公开业务 API 使用 `/api/v1`，`/health`、`/ready` 和 OAuth/OIDC 标准协议入口保留根路径；HTTP 大版本、认证 profile 版本、数据行版本分别管理。修改时同步实现、OpenAPI、测试及运行手册。
11. 一期采用“人员—公司—人员”协作：人员持有成员资格和任职，Agent 只是受监督的工具，不创建独立 Agent 成员、岗位、专用业务接口或委托链。技术服务认证不等于人员业务授权；自动化辅助不等于人工确认。独立 Agent/Delegation 与通用 ServiceGrant 后置，不能作为一期前置条件。见[一期协作边界](docs/people-company-collaboration.md)。
12. 业务模块和领域规则实现可以按受信插件边界组织，内部纯函数/DTO 无需逐个插件化。统一身份、授权强制入口、事务、幂等和持久事实协议不能由普通模块自行绕过；插件卸载不删除业务事实。当前静态路由、必需组件与固定 CommandSpec 集合不等于通用可配置插拔平台，新增/停用流程见[演进决策](docs/runtime-evolution-and-composition.md)。
13. 区分现行规格、实际代码与验收、历史提案。文档权威及状态见[设计入口](docs/README.md)；不能把目标类名或字段写成已经实现，不能用部分路径验收宣布整阶段完成。原始验证记录保留，派生摘要更正说明来源。

## 当前状态

2026-10-26 用户批准落实整体审计的全部五项 P2 与三项 P3（这是发现优先级，不是产品阶段）。[整改记录](docs/reviews/p2p3-architecture-closeout-2026-10-26.md)为后续入口：人员业务复用 `HumanOperations` / `CommandKernel.read`；管理公共协议用 `app.control`；当前原生会话事实用 `native_auth.current_session`。禁止重新跨域导入旧管理私有助手、跨测试模块共享 fixture 或绕开真实浏览器门禁。Runtime 成功历史有界，失败所有权不淘汰；组织 guard 保留一致性协议，容量以独立真实 PG 基线说明。交付用[冻结流程](deployment/dev/release-freeze.md)绑定精确提交、锁和迁移。


本轮用户已确认完成企业受控多应用 OAuth/OIDC，范围及合同见[功能矩阵](docs/oauth-functional-coverage.md)与[管理手册](deployment/identity/oauth-clients.md)。新应用默认 stage-userinfo audience，Stage 业务访问须显式 stage-api 配置和独立 profile/binding/MFA 准入。客户端注册、SSO/同意、RP 退出、本人会话与应用授权撤销不替代业务权限；Passkey/联邦/公开自动注册不在本轮。

2026-10-26 最新用户决定：自研满足实际需求的最小认证与 OAuth/OIDC 模块，先设计落档再实现和详细复测，见[现行设计](docs/native-authentication-design.md)。原生服务端实现、真库/浏览器复测与本机切换见[初次验收](docs/reviews/native-authentication-2026-10-26.md)；后续[深度审计](docs/reviews/native-authentication-audit-2026-10-26.md)修复因素重绑抢占和到期边界，重绑必须持单次证明，不能因种子为空退回密码引导。此决定取代长期默认依赖 Keycloak 的建议；复用成熟密码学/协议库，不自创算法。保留现有 Principal、成员、任职、组织 MFA 和业务事务边界；Keycloak 留作迁移对照，实际切换须有新链路验证。独立认证实现不能绕过组织当前权限，也不扩建完整 Keycloak 产品。

2026-10-26 代码复查与优先级见[本轮记录](docs/reviews/code-state-and-next-steps-2026-10-26.md)。Runtime/投递边界修复不改变阶段范围；随后 [P1b.4](docs/reviews/p1b4-assignments-mfa-2026-10-26.md)已实现受保护的本人任职发现、组织统一登录 MFA 策略和受控配置。用户选定密码＋TOTP；后续原生服务端因素绑定/恢复已交付；实际 Maestro 桌面客户端、生产认证部署与恢复流程另验。新 profile 不能给弱认证保留绕过强制 MFA 的准入路径；不得把未实现的管理 API 或通用注册平台当现有依赖。

P1b 当前进入 Stage 侧收尾与客户端联调准备：[复查记录](docs/reviews/p1b-closeout-2026-10-25.md)、[逐项 I1 证据](docs/reviews/p1b-identity-coverage-2026-10-25.md)、[Maestro 合同](docs/maestro-stage-p1b-handoff.md)。未启用和部分覆盖的 I1 项不能计为通过；桌面客户端与生产接入另验。组织 guard 保留当前串行策略，先依据独立 PG 并发基线和真实负载目标再评估细化锁。JWT 的内部 evidence TTL 不能当成远端 IdP 撤销可见上界。

“Stage 侧收尾”不等于完整客户端或生产验收。本人任职发现只返回当前人员和固定成员经历下的有效选择，不是权限票据；业务操作仍明确选一个任职。组织登录策略覆盖所有 human 身份/任职/工作项入口，校验精确 profile/version、ACR、AMR 和 auth_time 年龄，证据固定策略版本/摘要并在提交前重验；不得通过旧弱 profile 绕过。密码＋TOTP 已有隔离真实 IdP 和 Stage 链路证据，随后原生模块提供服务端 TOTP 绑定与恢复；实际 Maestro 交互与生产恢复流程仍未验收。P1c/P1d 的具体业务意图批准另行选择 WebAuthn/IdP step-up；登录 MFA 不构成业务批准。

P1a 已实现独立 Runtime Core、PG/Redis provider、可信注入的命令/投递组件及系统探针。P1b.1/.2 已有真实身份、成员、固定角色版本、scope/resource/direct ACL 与受控开发管理。P1b.3 当时以已有 WorkItem 的标题/说明编辑接通人员认证和命令提交；当前 P1b.4 另已开放本人任职查询并接入统一 MFA 策略。各轮范围与验证见[P1b.3 记录](docs/reviews/p1b3-human-work-2026-10-25.md)及[P1b.4 记录](docs/reviews/p1b4-assignments-mfa-2026-10-26.md)，目标约束见[身份表结构](docs/identity-schema-design.md)。

公开人员命令必须使用真实认证签发的短期证据，由 `AuthenticatedAuthorizationLoader` 联合当前授权，按 TrustState 共享锁→Principal 共享锁→组织 guard→授权/资源的顺序协调，并在提交前重验 profile/trust/外部映射、来源准入、成员、版本与截止时间。不能从请求 body 或 `/identity/me` 响应构造可信身份。只选一个明确任职，不合并岗位权限；元数据编辑要求显式 `task.metadata.update`、`owner_of`、draft/open 和预期版本。旧 fixture 仅供可信测试，旧角色不自动增加新 action，未映射旧行不授予权限。当前无公开创建工作项、授予任职或授权管理接口；开发数据与新角色 revision 由受控管理员显式安装。P1b.4 新增组织人员认证策略表，该轮为 26 张应用表；原生认证先新增八张表，多应用 OAuth 再新增 browser session/consent/logout request 三张，当前 37 张、head `c6f3a19d820b`；迁移将已有根回填 disabled/v1，仅由显式版本化配置启用 MFA，不因升级自动授予权限或强制 MFA。后续仍按一期推进 WASM 与签名准入，独立 Agent/Delegation 与通用 ServiceGrant 不作为门槛；不得把最小编辑操作计作完整 P2 任务协作、人工批准或生产验收。
