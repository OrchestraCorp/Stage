---
title: Stage 设计与实施入口
status: current-documentation-index
implementation_status: self-assignments-and-login-mfa-server-implemented-client-and-full-p1-pending
policy_effect: none
research_date: 2026-10-23
---

# Stage 设计与实施入口

当前新增[员工开户与一次性激活](employee-onboarding.md)，实现与复查见[本轮审计](reviews/employee-onboarding-2026-10-27.md)：显式启用管理角色 revision 2、待激活人员创建、本人设置密码／TOTP、重发撤销和安全结果恢复。迁移不会自动扩权；Maestro 管理页面、离职与生产交付仍另验。

前序[首个人员管理切片](human-administration-first-slice.md)：首管提名接受、已有人员目录、有限普通任职授撤、原键恢复及管理审计；对应 Maestro 体验与后续员工生命周期仍待交付。统一产品归属见[管理中心边界](maestro-stage-administration.md)。

当前代码的[架构全景与目录导航](stage-architecture-panorama.md)、[完整类/函数地图](stage-code-map.md)及[整体工程审计](reviews/stage-engineering-audit-2026-10-26.md)按工作区源码核对，区分已实现、内部底板和未来目标。

企业受控多应用 OAuth/OIDC 的当前完成范围见[功能矩阵](oauth-functional-coverage.md)，客户端登记/准入见[管理手册](../deployment/identity/oauth-clients.md)。

现行决定与实现见[自研最小认证设计](native-authentication-design.md)、[初次验收](reviews/native-authentication-2026-10-26.md)、[后续深度审计](reviews/native-authentication-audit-2026-10-26.md)和[运行手册](../deployment/identity/native-authentication.md)：Stage 同进程提供本地密码＋TOTP、会话与第一方最小 OAuth/OIDC，保留现有人员/成员/任职边界。[此前身份提供方建议](identity-provider-position.md)已被本轮明确决定取代，作为历史讨论保留。

2026-10-26 代码状态与下一轮优先级见[代码复查](reviews/code-state-and-next-steps-2026-10-26.md)：修复可选依赖环就绪隔离、已结束代次对象保留和非法外部回执；该记录提出的本人任职发现和 MFA 配置/校验随后由 [P1b.4](reviews/p1b4-assignments-mfa-2026-10-26.md)实现；真实客户端闭环仍待验收。旧复查保留其当时状态。

P1b 最新入口为[本人任职与登录 MFA](reviews/p1b4-assignments-mfa-2026-10-26.md)；前序证据包括[Stage 侧收尾复查](reviews/p1b-closeout-2026-10-25.md)、[身份覆盖矩阵](reviews/p1b-identity-coverage-2026-10-25.md)和[Maestro 联调合同](maestro-stage-p1b-handoff.md)。服务端准备与桌面验收分开记录，不将部分 I1 覆盖写成完整一期完成。

当前已验证的是组件内核、真实人员身份、最小工作项授权链，以及本人任职发现和组织统一登录 MFA 服务端路径。用户选定密码＋TOTP，已以隔离真实 IdP 验证因素和 Stage 强度/新鲜度约束；**Maestro 桌面交互、生产接入、WASM、具体业务批准与签名准入仍未完成**。新原生模块补齐服务端因素绑定、恢复、退出及账户安全；脚本与服务端证据不替代实际 Maestro 桌面交互和生产恢复验收，业务模块可以受控插件化，但通用能力注册/蓝图与任意在线插拔尚未实现。本轮文档准确性检查见[定位与路线复查](reviews/documentation-alignment-2026-10-25.md)。

**最新范围对齐：一期为“人员—公司—人员”。** Agent 是人员监督下的工具，不单独建设成员、岗位、业务接口或委托体系；技术 Worker 身份不自动取得人员业务权限。用户已恢复开发并选择已有工作项的标题/说明编辑作为首条命令。P1b.3 接通真实认证、当前单任职、事务内重验与结果恢复；独立 Agent/Delegation 与通用 ServiceGrant 继续后置。见[一期协作边界](people-company-collaboration.md)与[P1b.3 记录](reviews/p1b3-human-work-2026-10-25.md)。

2026-10-25 已明确产品边界：**一套 Stage、一个组织授权根、一套专属数据库、一个部署环境**。组织授权根是信任与治理边界，不等同于一个法人公司；集团、子公司、部门等业务组织单元以后按需建模，部门和项目的权限使用 scope，scope 不自动等同组织树。既有 `tenant_*` 存储名称保留兼容，含义收敛为组织根。公开业务 API 使用 `/api/v1`，当前接口不提供组织选择或 SaaS 多租户管理。详见[组织授权根语义](organization-root-semantics.md)和[私有化部署与 API v1](private-deployment-api-v1.md)。旧 reviews 中的多租户用例记录保留为当时的验证证据，不再定义当前产品目标。

组织根改名与幂等兼容的冻结基线见[落实记录](reviews/organization-root-semantics-2026-10-25.md)：当时 PG＋Redis 487 项、SQLite 464 项、在线冒烟 5 项、真实 IdP 18 项通过。基于 `8044717` 的当前人类内部授权开发与实际检查另见[P1b.2 记录](reviews/p1b2-authorization-2026-10-25.md)，不累计或改写历史计数。

OrchestraCorp 的长期愿景是 **AI 驱动、安全可靠的分布式工作协作平台**。Stage 用 Python 自研可信运行底座：参考 Cordis 建立组件内核，未来业务流程运行器共享能力、身份、版本和执行契约。Graphon / Dify 仅作为研究参考，不作为产品依赖。第一期优先贯通 **Core＋真实身份认证与当前授权＋受控 WASM＋最小签名准入**，用资格评估命令验证共同基座；完整任务闭环保留到后续业务切片。

设计与实现按阶段区分。P1a 的 Core、生命周期和装配验收见[P1a 验证](reviews/p1a-validation-2026-10-25.md)；真实身份首条切片见[身份记录](reviews/p1b-first-slice-2026-10-25.md)；角色/资源 loader 见[P1b.2](reviews/p1b2-authorization-2026-10-25.md)；人员认证、元数据命令与恢复见[P1b.3](reviews/p1b3-human-work-2026-10-25.md)。历史计数保留各自范围，本轮不以最小编辑操作声明 WASM、签名、完整 P1 或 P2 已完成。

2026-10-25 用户确定 **PostgreSQL＋Redis** 长期存储组合，本轮采用 PostgreSQL 17 / Redis 8 工程基线；PostgreSQL 是唯一主库，Redis 是可重建缓存/非权威短期协调/可丢失唤醒。新[存储策略](storage-strategy.md)取代早期 MySQL 默认与双库兼容要求；历史迁移和验证记录继续保留原范围。

## 阅读顺序

先读[当前代码架构](current-code-architecture.md)与[最新人员任职/MFA 记录](reviews/p1b4-assignments-mfa-2026-10-26.md)，再按[阶段路线](roadmap.md)和专项规格确定下一步。[2026-10-24 对齐审查](reviews/code-design-alignment-2026-10-24.md)基于历史提交 `eb11e43`，保留其当时问题及证据，不作为当前代码范围。

| 文档 | 回答的问题 |
|---|---|
| [Maestro 管理中心与 Stage 边界](maestro-stage-administration.md) | 用户确认的完整管理体验归属、页面范围、管理API准备和两端共同验收 |
| [企业身份与管理完备性评估](reviews/enterprise-identity-completeness-2026-10-26.md) | 已实现认证／权限底座与管理员产品、人员生命周期、恢复运营缺口的区分 |
| [并发优化实现与复查](reviews/concurrency-optimization-2026-10-26.md) | 显式协调协议、真实 PG 竞争、浏览器回归和容量对比 |
| [Stage 授权与业务并发改进提案](stage-concurrency-improvement-plan.md) | 验证去重、组织读写保护、幂等同步及后续路线约束；已实现，验收另见记录 |
| [成熟 OA 的身份、权限与并发调研](reviews/oa-identity-permission-concurrency-research-2026-10-26.md) | O2OA、Frappe/ERPNext/HR、Flowable、RuoYi 固定源码对照；研究候选，不是新增业务承诺 |
| [组织锁与成熟方案调研](reviews/organization-lock-industry-research-2026-10-26.md) | PostgreSQL、Zanzibar、SpiceDB/OpenFGA 的一致性与并发做法；研究候选，不是已批准实现 |
| [P2/P3 整改复查](reviews/p2p3-architecture-closeout-2026-10-26.md) | 审计 A–H 的公共入口、HTTP 分工、浏览器 CI、冻结、容量与共享协议整改 |
| [公共受保护业务入口](protected-business-operations.md) | 新业务如何复用 HumanOperations 与 CommandKernel.read，不复制安全装配顺序 |
| [交付冻结与浏览器门禁](../deployment/dev/release-freeze.md) | 精确提交、锁文件、迁移、源码/包制品与拒绝跳过的 Chromium CI |
| [一期人员—公司—人员协作边界](people-company-collaboration.md) | 人员责任、公司规则、受监督工具、人工确认与近期实施范围 |
| [愿景与自研运行时方向](vision-and-runtime-direction.md) | OrchestraCorp 的目标；Stage 自研范围；可验证分布式组织怎样分阶段建设 |
| [定位与路线文档复查](reviews/documentation-alignment-2026-10-25.md) | 现行/历史权威、插件化程度、登录 MFA 和阶段门槛的本轮纠正 |
| [2026-10-24 设计核对](design-review-2026-10-24.md) | 历史设计阶段的依据与表达修正，不代表当前实现 |
| [目标架构](architecture.md) | 组件如何装配；请求怎样执行；运行时与业务事务的边界在哪里 |
| [当前代码架构与 Cordis 对照](current-code-architecture.md) | 当前模块关系图、默认装配范围、Cordis 共享机制与 Stage 差异 |
| [运行时演进与能力组合决策](runtime-evolution-and-composition.md) | ComponentContext 边界、三层运行时、P1～P5 演进、能力组合、Dify DSL 取舍与短期禁止事项 |
| [一期基础框架实施规格](framework-foundation.md) | P1a 公共接口、owner/依赖图、停止与排空、事件调用保护、provider 接入及 12 个补充边界场景 |
| [Live Smoke Tests](../tests/smoke/README.md) | 对运行中的 Stage 做只读健康、就绪、并发、OpenAPI、PostgreSQL 和 Redis 冒烟检查 |
| [PostgreSQL 与 Redis 存储策略](storage-strategy.md) | 2026-10-25 选型；主库权威、缓存撤权、故障与事务边界；历史 MySQL 退出支持 |
| [第一期范围与验收](phase-1.md) | 第一阶段具体交付什么、默认规则是什么、怎样判断完成 |
| [核心对象与契约](core-object-contracts.md) | 能力、身份、命令结果、流程实例、发布清单的关系和权威边界 |
| [身份认证与授权](identity-and-authorization.md) | 人员认证、主体映射、任职、当前授权、批准与撤销；独立 Agent 委托候选后置 |
| [组织授权根语义](organization-root-semantics.md) | 一实例一个授权/信任/治理根；组织单元、scope、法人和跨 Stage 身份的边界 |
| [Maestro 多 Stage 连接与切换](maestro-multi-stage-connections.md) | 多套 Stage 连接、独立会话、切换、缓存和未知结果恢复合同 |
| [企业私有化部署与 API v1](private-deployment-api-v1.md) | 专属数据库、固定组织根、API 大版本与个人资料职责；当前改动和验收记录 |
| [P1b 实施入口](p1b-implementation-entry.md) | 身份管理、认证与授权的起步切片、现有代码接缝、最少待决项及 Maestro 联调边界 |
| [P1b Stage 侧收尾](reviews/p1b-closeout-2026-10-25.md) | 身份/权限代码复查、输入修复、恢复隔离、组织锁并发基线及验收边界 |
| [P1b 身份覆盖矩阵](reviews/p1b-identity-coverage-2026-10-25.md) | 逐项 I1 实现和证据、部分覆盖、未启用与后置项 |
| [Maestro 联调合同](maestro-stage-p1b-handoff.md) | 登录配置、测试任职、读写和原键恢复、错误处理、注销与撤权 |
| [P1b.2 人类授权实施记录](reviews/p1b2-authorization-2026-10-25.md) | 六张新授权表、数据库 loader、命令锁协调、受控开发写入及未开放 HTTP 的实际范围 |
| [P1b.3 人员命令实施记录](reviews/p1b3-human-work-2026-10-25.md) | 真实人员认证、明确单任职、已有工作项元数据读写、提交前重验、认证/授权审计与原 key 恢复 |
| [P1b.4 本人任职与登录 MFA](reviews/p1b4-assignments-mfa-2026-10-26.md) | 本人任职分页、组织统一强度/新鲜度、策略变更与提交竞争、真实协议验收边界 |
| [登录 MFA 开发管理](login-mfa-development.md) | 密码＋TOTP 的职责、组织策略、受控配置、恢复、迁移和客户端接缝 |
| [人类授权开发操作](authorization-development.md) | dev/test 安装角色版本/任职/ACL、五类撤销、原幂等键结果恢复与内部装配边界 |
| [身份与授权表结构对齐稿](identity-schema-design.md) | 一期人员认证/成员/任职/资源授权，以及明确后置的 ServiceGrant/Agent/Delegation 草案 |
| [P1b 首条切片实施记录](reviews/p1b-first-slice-2026-10-25.md) | 本轮真实认证、来源准入、成员读取、bootstrap 和 PG 竞争检查的实际交付范围与验证证据 |
| [P1b 身份切片复查](reviews/identity-first-slice-review-2026-10-25.md) | 错误响应缓存、管理环境误用、身份请求时间预算的修复及完整回归 |
| [组织授权根语义落实记录](reviews/organization-root-semantics-2026-10-25.md) | 一实例一个授权/信任/治理根与组织单元、scope、法人、跨 Stage 身份边界的本轮对齐 |
| [WASM 宿主与隔离执行](wasm-host-design.md) | 固定ABI、独立runner、制品准入、全过程预算和业务提交边界 |
| [后续任务业务切片](business-slice-plan.md) | 基座完成后的任务、承诺、容量、交付与独立验收设计 |
| [长期目标与演进路线](roadmap.md) | Loader、配置协调、工作流、多进程和代码更新怎样逐步实现 |
| [Runtime 一致性工作规格](runtime-conformance.md) | “符合 Cordis 思路”具体指哪些行为；与参考实现有什么差别 |
| [调研依据与设计决策](research.md) | 论文、DSH 源码和 Python 官方文档提供了哪些证据；哪些仍未证明 |
| [Dify 工作流编排对照](dify-workflow-study.md) | 可视蓝图、节点目录、发布快照与暂停恢复如何实现；哪些机制适合 Stage |
| [Stage 与 Z Pool 历史架构对照](z-pool-architecture-comparison.md) | 研究时点的继承与差异；现行实现以当前代码架构为准 |
| [Z Pool 工程能力复用范围](z-pool-reuse-plan.md) | 哪些P1基础应继续使用、定向提炼或重新设计，以及对应验收条件 |
| [架构图源文件与导出](diagrams/README.md) | 可编辑 Mermaid、SVG 和 PNG 的位置及生成方式 |

## 架构图

![Stage 一期目标与长期扩展](diagrams/stage-architecture.png)

图中蓝色区域是一期目标，其中 Core 和最小人员路径已有实现，其他部分按阶段建设；紫色虚线区域是后续目标。图不是当前代码部署图；当前实现以[当前代码架构](current-code-architecture.md)及对应验收为准。

## 本轮确定的设计方向

1. 保留 Python 3.12、FastAPI、SQLAlchemy async、Alembic；独立 `app/runtime/` 已实现，业务模块复用当前授权与事务协议。
2. 用 `ComponentContext` 管组件依赖和资源，用 `ExecutionContext` 管每次业务动作；现有 `AuthorizationContext` 是后者的可信授权输入投影。
3. 一期采用受信任的固定Python组件组合＋受控WASM子进程；Core支持程序化服务启停和依赖换代。WASM只允许经准入的有限纯计算模块，不提供通用插件上传或在线源码HMR。
4. 评估结果、命令终态、审计和outbox同事务；后续任务业务复用这一约束。计算不持有业务锁，提交前重验当前权限、制品及事实版本。
5. Cordis 对齐采用固定参考版本、逐场景对照和公开偏差。`stage-runtime-core-v1` 是 Stage 工作规格，不是官方认证。
6. 一期必须验证真实协议adapter与隔离测试IdP，fixture仅作辅具；开发签名根和虚构数据不能代替生产身份、密钥、隔离和运维验收。
7. 自研组件内核已实现，持久流程运行器按 P3 建设；目标是共用能力定义、身份、版本和结果契约，分别管理运行资源与业务实例。分布式组织不以区块链为前提，多节点部署按阶段验收。
8. PostgreSQL 17 保存当前授权、幂等、审计/outbox、业务租约和业务事实；Redis 8 只辅助，清空不丢事实，不承诺跨库双写原子性，缓存不得绕过当前撤权。SQLite 仅快测，MySQL 不再是受支持生产配置。

## 与已有文档的关系

本目录是现行设计入口。用户已确认的产品约束和 [AGENT.md](../AGENT.md) 优先；[路线](roadmap.md)与[一期方案](phase-1.md)定义阶段范围，专项规格定义该阶段的合同，[当前代码架构](current-code-architecture.md)、代码和带环境的验证记录说明实际实现。目标规格不能当作完成证明，当前代码缺少某项安全要求也不能反过来使该要求自动失效。

根目录早期提案、research 和 Z Pool 对照均标为历史；旧字段、公式、范围和测试数量只说明当时状态。日期较新的文档也必须区分 `implemented`、目标、未启用和后置，不能仅凭文件日期覆盖现行决定。新设计修改应同步主入口与受影响专项，保留原始验收证据；派生摘要有误时说明更正原因，不改原始结果冒充重跑。

当前实现证据：[P1a 验证](reviews/p1a-validation-2026-10-25.md)、[P1b 首条身份记录](reviews/p1b-first-slice-2026-10-25.md)、[P1b.2 内部授权记录](reviews/p1b2-authorization-2026-10-25.md)、[P1b.3 人员命令记录](reviews/p1b3-human-work-2026-10-25.md)与[P1b.4 任职/MFA 记录](reviews/p1b4-assignments-mfa-2026-10-26.md)。默认装配已有 RuntimeHost、系统 HTTP、身份、本人任职发现及人员工作项服务；元数据读写/结果查询已接入当前鉴权。可靠投递仍需可信 authorizer/sink，WASM 与签名准入继续后续阶段。

- [组织运行架构初稿](../architecture.md)：历史组织责任与能力域背景，统一 RuntimeContext 和早期范围不作现行要求。
- [可组合后端提案](../composable-runtime-design.md)：第 2 节统一 RuntimeContext 和第 5.1 节混合 effect 分类由本目录的双上下文、运行资源 / 持久业务副作用分层细化替代。
- [OA 组合策略](../composition-strategy.md)：保留组合思想；“预建大部分 OA”不作近期交付要求，先以最小业务闭环验证模块边界。
- [授权模型设计](../authorization-model-design.md)：保留三类授权研究；旧并集公式不作现行决策规则，当前需单任职 action、对象关系与 ABAC 同时满足。
- [2026-10-19 框架审查记录](framework-review-2026-10-19.md)：保留为历史证据，不把其中的底板测试误记为新 runtime 已验收。

早期设计与图表验证不计实现验收；历史[存储切换验证](reviews/storage-switch-validation-2026-10-25.md)只证明该轮范围，后续 Core、身份及最小人员命令分别以上述记录为准。生产认证部署、具体部署目标和容量业务口径仍按[待决事项](roadmap.md#open-decisions)确定。
