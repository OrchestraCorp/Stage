---
title: 组织授权根语义落实记录
status: documentation-and-schema-alignment
review_date: 2026-10-25
policy_effect: none
---

# 组织授权根语义落实记录

本轮针对私有化部署边界完成一次概念和文档复盘。结论是：Stage 的部署单元只能有一个**组织授权根**，但组织授权根不等于一个法人公司，也不预先限制根下未来可以登记多少集团、子公司、事业部或其他业务组织单元。

## 已确定的语义

- `organization_root` 是实例的信任、治理、授权 epoch、审计和数据库边界。
- `organization_unit` 是未来可按业务需要建设的集团/子公司/部门等组织主数据；它不自动创建授权，也不自动继承权限。
- `authorization_scope` 是授权适用范围；它可以引用组织单元、项目或资源集合，但不要求是组织树。
- `legal_entity` 是未来按领域需要建设的法人/结算主体；它不替代组织根、认证根或数据库隔离。
- 一个 human Principal 在一个 Stage 内可以拥有多个成员经历和责任身份；一次操作选择明确 assignment、grant 或 delegation，不默认合并全部任职。
- 两个独立 Stage 各自保存本地 Principal、成员、任职、撤销和审计。Maestro 可以连接多个实例，但不能跨实例传递 token、角色或授权结果，Stage 也不建立跨企业全局身份。

## 已落地的存储边界

迁移 `d6a3c5f8e902`（from `c4f2a89d1e63`）完成无损物理重命名：

```text
tenant                   → organization_root
tenant.tenant_ref        → organization_root.organization_ref
```

迁移不重建或合并数据，保留原主键、约束和引用。`tenant_membership`、`tenant_authentication_binding`、`execution_tenant_guard` 及其他 `tenant_*` 字段暂时作为兼容物理名称；它们表示组织根内的成员、认证绑定和授权串行化行，不表示 SaaS 租户控制面。

根的 `deployment_slot = 1` 单例约束只限制一个数据库中的授权根数量。它不限制根下未来的组织单元或法人数量，也不授予根内主体全局业务权限。

离线管理输入改用 `organization_ref` 和 `kind: organization`，成员撤销也使用 `organization_ref`。旧输入被明确规范化接受，双字段冲突拒绝。新旧输入维持历史 canonical digest（包括可选 null）；持久 `revoke.tenant` 操作名和审计 JSON 保留，结果查询同时接受新旧操作名并返回组织语义投影。这个兼容层用于保持历史幂等与未知结果恢复，不表示继续提供租户控制面。

## 已同步的规范入口

- [组织授权根语义](../organization-root-semantics.md)：术语、表结构边界、API、Maestro 多实例边界和后续建设规则。
- [私有化部署与 API v1](../private-deployment-api-v1.md)：组织根解析和 `organization_ref` 响应字段。
- [身份与授权契约](../identity-and-authorization.md)：成员、任职、委托、认证来源和跨实例身份边界。
- [身份表结构对齐稿](../identity-schema-design.md)：兼容 `tenant_*` 字段与组织根语义的关系。
- [当前代码架构](../current-code-architecture.md)、[架构图源](../diagrams/stage-current-code.mmd)：当前实现中的组织根投影。

AGENT、README、contracts、开发运行说明和一期文档已经改为使用“一个组织授权根”的表达。历史 review 记录未改写；它们仍保留各自当时的范围和证据，并由现行规范解释当前语义。

## 当前范围与后续门槛

本轮只完成语义、文档和根表命名对齐，没有新增组织单元、法人、人员管理或跨实例身份功能。后续增加 `organization_unit`、`legal_entity` 或更细的 scope 之前，必须明确其权威来源、授权继承规则、审计读者和与根的外键边界；不能把它们塞回 `organization_root`，也不能用 scope 字符串猜测组织归属。

## 迁移与运行验证

开发库应用 `d6a3c5f8e902` 后，Alembic `check` 未发现 schema 漂移。迁移前备份位于仓库外 `stage-before-organization-root.dump`，逐条快照位于私有状态目录 `before-organization-root.json`；组织根、成员、认证绑定、主体、bootstrap、身份控制记录和 guard 均与迁移前逐条一致。

使用原 bootstrap 幂等键，分别以旧 `tenant_ref` 和新 `organization_ref` 计划重放，均恢复同一原操作；再次比对相关表没有变化。独立 PostgreSQL 空库完成全历史 `upgrade head` → `check` → `downgrade base` → `upgrade head` → `check` 五项往返检查。

真实 Keycloak → `/api/v1/identity/me` 的 18 项身份链路检查通过，公开接口路径、字段形状和 HTTP 大版本保持兼容。最终结果：

| 验证项 | 本轮结果 |
|---|---|
| SQLite 全套 | 464 passed，20 skipped；只证明快测范围 |
| PostgreSQL 17＋Redis 全套 | 487 passed，5 skipped |
| 在线系统冒烟 | 5 passed |
| 真实 Keycloak → v1 身份 API | 18 项检查通过 |
| 静态与合同 | Ruff、116 个文件格式检查、113 个源文件 mypy、OpenAPI 合同及 `git diff --check` 通过 |
| 文档 | 22 份 Markdown、250 个本地链接、代码围栏、当前架构内联 Mermaid 与源文件同步检查通过 |
| 图导出 | 固定 Mermaid 11.17.2＋已有 Playwright/Chromium 重新生成目标 SVG/PNG，视觉核验组织根文字与连线完整；未新增项目依赖 |
| 开发运行 | Stage、PostgreSQL、Redis、测试 IdP 四个服务 active |

历史 436/460/477 等数字继续对应各自 review 或前序切片，不作为本轮结果；跳过用例不计为通过。本轮验证没有覆盖尚未接入身份 loader 的旧 CommandKernel 业务写路径，也不表示组织单元、法人、角色管理或跨实例身份已经实现。
