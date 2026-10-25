# Stage Contracts

`openapi.yaml` 是当前 HTTP 契约入口。新增或修改 API 时，同步更新实现、契约、稳定 operationId 和测试。

业务接口使用 URL 大版本 `/api/v1`；系统探针 `/health`、`/ready` 保留根路径。当前冻结 `GET /api/v1/identity/me`，operationId 为 `identity_get_self`。同一大版本保持兼容，破坏性字段或行为变更进入新大版本；未实现的 `/api/v2` 返回 404。

Stage 是企业私有化部署。每个实例由服务端唯一组织授权根决定；该根是信任和治理边界，不等同于一个法人公司，也不提供 SaaS 租户选择。身份 query 仅接受 `profile_ref/profile_version`，响应使用 `organization_ref`；额外 query（含 `tenant_ref/organization_ref`）返回 422。尚无正式客户端的首版冻结同步删除旧 `/api/identity/me`，不提供旧路由或重定向。组织根、组织单元与 scope 的边界见[组织授权根语义](../docs/organization-root-semantics.md)。

HTTP URL 大版本、OpenAPI `info.version: 1.0.0` 合同发行号、OpenAPI 规范版本、认证 `profile_version` 和数据行 `version` 含义分别定义，不能相互替代；项目包版本仍单独管理。个人资料 CRUD、人员列表、角色管理和工作命令尚未公开；对应领域稳定后再加入合同。细则及当前范围见[私有化部署与 API v1](../docs/private-deployment-api-v1.md)。
