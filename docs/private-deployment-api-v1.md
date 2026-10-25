---
title: 企业私有化部署、个人管理边界与 API v1
status: accepted-design
implementation_status: private-deployment-api-v1-implemented-and-verified-personal-profile-pending
review_date: 2026-10-25
policy_effect: none
---

# 企业私有化部署、个人管理边界与 API v1

2026-10-25 用户明确 Stage 面向企业私有化部署，暂不建设 SaaS 式多租户控制面；同时确认 HTTP 接口采用版本化路径。这里的私有化边界是“一实例一个组织授权根”，根是信任与治理边界，不等同于一个法人公司。集团、子公司、部门等业务组织单元以后按需建模。完整术语见[组织授权根语义](organization-root-semantics.md)；历史 reviews 保留原时间、代码范围和验证证据。

## 部署与组织

- 一套 Stage 服务一个组织授权根，使用专属数据库和一个固定部署环境。根可以服务一个简单企业，也可以在明确治理政策下承载集团、子公司等业务组织单元；开发、测试、生产分别部署，不建立共享数据库 SaaS 租户控制面。
- 根由服务端数据库中的唯一记录决定；客户端无需选择组织，也不能通过 query、header 或 body 改变部署组织。部门、团队、项目使用 scope；scope 是授权范围，不自动等同组织树，也不创建第二根。
- 根表使用 `organization_root.organization_ref`；`tenant_membership`、`tenant_authentication_binding`、`execution_tenant_guard` 和它们的 `tenant_ref` 等历史物理名称继续兼容，语义为组织根内成员/认证/授权。身份 ORM 与公开 API 使用 `organization_ref`。
- 根表 `organization_root.deployment_slot` 使用 `SmallInteger NOT NULL DEFAULT 1`、`CHECK (deployment_slot = 1)` 和唯一约束，数据库至多一条组织根。没有根记录时身份入口拒绝访问；根停用仍保留历史引用，不能通过新建根绕过停用。
- 无损迁移 `d6a3c5f8e902`（from `c4f2a89d1e63`）将根表物理名称改为 `organization_root`，并将根列改为 `organization_ref`；前序 `c4f2a89d1e63` 负责多根预检和单例约束，发现历史多根时明确失败，不合并、不删数据、不挑选第一条。既有根和引用保留原 ref。部署环境由离线 bootstrap/revoke 的管理检查负责：在管理锁内拒绝第二环境或历史混合环境，并保留幂等结果约束；该环境检查不属于根表重命名迁移。

单根不会取消权限边界。主体类型、成员当前性、任职、组织单元/项目范围、资源关系、委托、认证来源和撤销仍由可信服务检查。数据库单例限制与授权检查分别负责不同事实；根单例不表示根下只有一个法人或所有成员拥有全局权限。

## HTTP 合同

```http
GET /api/v1/identity/me?profile_ref=local-keycloak-human&profile_version=1
Authorization: Bearer <access_token>
```

| 项目 | 当前约定 |
|---|---|
| 路由与客户端方法 | `/api/v1/identity/me`；稳定 `operationId: identity_get_self` |
| query | 仅 `profile_ref`、`profile_version`；额外 query，包括 `tenant_ref`、`organization_ref`，返回 422 |
| 组织字段 | 响应为服务端确定的 `organization_ref`；不再公开 `tenant_ref` 或 `organization_ref` 选择参数 |
| 其他身份字段 | `principal_ref`、`principal_type`、`membership_ref`、`membership_version`、`authenticated_workload_ref`、`authentication_valid_until` |
| 首版切换 | 尚无正式客户端，旧 `/api/identity/me` 删除且返回 404；不保留别名或重定向 |
| 未支持版本 | `/api/v2/identity/me` 返回 404 |
| 系统探针 | `/health`、`/ready` 保留根路径 |
| 缓存 | 身份路径成功及错误响应使用 `Cache-Control: no-store`；v1 版本迁移不得遗漏错误处理 |

`profile_ref/profile_version` 只引用服务端预先登记的认证配置。它们不能动态注册 issuer、绕过企业来源准入或改变成员关系。当前身份响应不包含任职/权限列表，不充当后续命令的授权凭证。

版本分别管理：URL `v1` 是 HTTP 兼容性边界；OpenAPI `info.version: 1.0.0` 是合同发行号；`openapi: 3.1.0` 是文档规范；认证 `profile_version` 固定验证语义；数据行 `version` 与 epoch 用于当前性和并发。项目包版本也独立管理。同一 API 大版本内保持兼容，破坏性字段或行为变化使用新大版本。

## 个人管理的数据职责

本轮固定个人管理的设计边界，尚未增加资料表或 CRUD 接口。当前已实现的是安全身份及企业成员经历，不等于个人中心完成。

| 层 | 现有/规划模型 | 所属字段与写入规则 |
|---|---|---|
| 安全主体 | 已有 `principal`、`external_identity` | 主体类型、状态、认证关联、epoch；通过受控身份管理变更，个人资料编辑不能修改 |
| 人类展示资料 | 规划 `human_profile`，与 human Principal 一对一 | 显示名、受控头像资源引用、locale、IANA timezone、version；未来只允许本人或明确获权管理员修改，不以邮箱/昵称授予身份 |
| 组织根内成员经历 | 已有 `tenant_membership`；未来按需增加成员档案 | 成员资格、生效/终结时间与来源；工号、组织单元归属和企业内称谓等由对应业务模型管理，不能写回全局个人资料以改变资格 |
| 任职和资源范围 | 规划角色版本/任职及 scope 授权 | 部门/项目岗位、能力和委托；不通过个人资料接口编辑 |
| 凭据与登录会话 | 企业选定 IdP | 密码、MFA、凭据恢复与会话由 IdP 管理；Stage 不在个人资料中保存密码或 token |

后续个人资料写接口应在具备“只写自身”、expected_version、幂等、受控审计的事务闭环后公开；读取按实际需要投影。任职/服务/Agent 授权仍按 [P1b 实施顺序](p1b-implementation-entry.md)推进，本轮不提前公开人员列表、邀请、账号关联或任职管理接口。

## 验证与本轮范围

以下表格保留 API v1 首次切换时的验证记录。组织授权根语义、`d6a3c5f8e902` 无损重命名及最新完整验收见[组织根落实记录](reviews/organization-root-semantics-2026-10-25.md)。

当前实现需覆盖 v1 合同、额外组织参数拒绝、旧路由与未支持版本拒绝、组织根单例及无损迁移、离线初始化幂等和单环境、身份撤销/到期以及真实 IdP 链路。组织单元和部门/项目 scope 的业务授权验收随后续 loader 实施，不以本轮身份读取代替。

更新后的真实 IdP 冒烟使用 `--organization-ref stage-development` 核对预期响应；这个 CLI 参数只用于断言，不加入 HTTP 请求。执行命令见[云设备运行说明](../deployment/dev/README.md)和[IdP 说明](../deployment/identity/README.md)。

2026-10-25 本轮验证结果：

| 验证项 | 结果 |
|---|---|
| SQLite 全套 | 454 passed，20 skipped；只证明快测范围 |
| PostgreSQL 17＋Redis 全套 | 477 passed，5 skipped |
| 在线系统冒烟 | 5 passed |
| 真实 Keycloak → v1 身份 API | 18 项检查通过 |
| 静态与合同检查 | Ruff、116 个文件格式检查、113 个源文件 mypy、OpenAPI 合同检查及 `git diff --check` 通过 |
| 文档检查 | 19 份 Markdown 相对链接与代码围栏检查通过 |
| 开发库迁移与运行 | 应用 `c4f2a89d1e63`，schema 无漂移；重启后 `/ready` 与完整 v1 身份链路正常 |
| 独立 PostgreSQL 空库迁移往返 | `stage_test_private_api_migration` 完成 `upgrade head` → `check` → `downgrade base` → `upgrade head` → `check`，五项通过 |

迁移前备份位于仓库外 `/root/.local/state/orchestra-stage/identity/stage-before-private-api-v1.dump`。已有开发组织、人类/服务主体及成员记录均保留，没有重新 bootstrap。此备份和开发机验证不替代生产恢复演练；跳过的用例不计为通过。历史 436/460 等数字继续对应原 reviews，不作为本轮结果。
